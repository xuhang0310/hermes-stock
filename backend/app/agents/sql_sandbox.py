"""数字人 run_sql 只读沙箱：语法树校验 + 按用户改写 + 只读会话执行。

多用户隔离不靠字符串过滤（子查询、CTE、注释都能绕过表名黑名单），而是：

1. sqlglot 按 MySQL 方言把 SQL 解析成语法树，只接受单条 SELECT / UNION 查询；
2. 遍历语法树里的每一个表引用（含子查询、CTE、UNION、LATERAL 内部）：
   - 共享研究表（行情、因子、自动选股、知识库）原样放行；
   - 个人数据表替换为 ``(SELECT * FROM 表 WHERE 归属列 = 当前用户) AS 原别名``，
     查询写法不变，但只能看到当前用户自己的行；
   - 其余表（账号、密钥、日志、运行时内部表）及带库名的跨库引用一律拒绝；
3. 实际执行的是由语法树重新生成、去掉注释与优化器提示的 SQL，而不是原始字符串，
   MySQL ``/*! ... */`` 可执行注释里夹带的语句进不了数据库；
4. 独立连接池，会话级只读事务 + 单语句超时；结果最多返回 max_rows 行。

用户身份由调用方显式传入（Agent run 的 user_id），管理员也只看自己的数据，与
结构化工具的 data_user_scope 语义一致。
"""
from __future__ import annotations

import threading
import time
import weakref
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

import sqlglot
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlglot import exp
from sqlglot.errors import ParseError

from backend.app.core.database import Base, create_engine_for_url
from backend.app.core.settings import settings

STATEMENT_TIMEOUT_MS = 15_000
SQL_CHAR_LIMIT = 20_000
CELL_CHAR_LIMIT = 2_000

# 共享研究数据：所有用户可见、不含账号与密钥。
SHARED_TABLES: frozenset[str] = frozenset({
    "instruments",
    "daily_bars",
    "stock_daily_basic",
    "stock_latest_prices",
    "stock_adj_factors",
    "market_snapshots",
    "market_trading_calendar",
    "tushare_factor_catalog",
    "tushare_factor_values",
    "automatic_selection_strategies",
    "automatic_selection_runs",
    "automatic_selection_candidates",
    "automatic_selection_evaluations",
    "automatic_selection_stock_snapshots",
})

# 共享知识库：剔除早期镜像进 documents 的个人 Agent 报告（doc_type='report'），
# 与 rag_ingest 不再摄取 report 文档的口径一致。
_PUBLIC_DOCUMENT_IDS = "SELECT id FROM documents WHERE doc_type <> 'report'"
SHARED_ROW_FILTERS: dict[str, str] = {
    "documents": "doc_type <> 'report'",
    "document_chunks": f"doc_id IN ({_PUBLIC_DOCUMENT_IDS})",
    "document_symbols": f"doc_id IN ({_PUBLIC_DOCUMENT_IDS})",
}

# 个人数据表 -> 归属用户列：自动只返回当前用户的行。
PERSONAL_TABLES: dict[str, str] = {
    "positions": "owner_user_id",
    "position_accounts": "owner_user_id",
    "position_holding_snapshots": "owner_user_id",
    "position_trades": "owner_user_id",
    "position_import_batches": "owner_user_id",
    "watchlist_groups": "owner_user_id",
    "watchlist_items": "owner_user_id",
    "events": "owner_user_id",
    "agent_reports": "owner_user_id",
    "risk_checks": "owner_user_id",
    "agent_tasks": "owner_user_id",
    "backtest_runs": "owner_user_id",
    "paper_accounts": "user_id",
    "paper_positions": "user_id",
    "paper_orders": "user_id",
    "screener_saved_strategies": "user_id",
    "inbox_notifications": "user_id",
    "trade_plans": "owner_user_id",
    "trade_plan_items": "owner_user_id",
    "market_events": "owner_user_id",
}

# 归属列为空的行是公共数据（宏观/市场事件、系统回测），所有用户可见；
# 与 StockRepository.events / backtest_engine / digital_human_tools 的口径一致。
PUBLIC_WHEN_UNOWNED: frozenset[str] = frozenset({"events", "backtest_runs"})

# 自身无归属列、经父表归属的个人数据：表 -> (外键列, 父表)。
PERSONAL_CHILD_TABLES: dict[str, tuple[str, str]] = {
    "event_evidence": ("event_id", "events"),
}

# 有副作用、阻塞或泄露连接身份的函数。
_FORBIDDEN_FUNCTIONS: frozenset[str] = frozenset({
    "SLEEP",
    "BENCHMARK",
    "LOAD_FILE",
    "GET_LOCK",
    "RELEASE_LOCK",
    "RELEASE_ALL_LOCKS",
    "IS_FREE_LOCK",
    "IS_USED_LOCK",
    "MASTER_POS_WAIT",
    "SOURCE_POS_WAIT",
    "WAIT_FOR_EXECUTED_GTID_SET",
    "WAIT_UNTIL_SQL_THREAD_AFTER_GTIDS",
    "USER",
    "CURRENT_USER",
    "SESSION_USER",
    "SYSTEM_USER",
    "CONNECTION_ID",
    "SYS_EXEC",
    "SYS_EVAL",
})

_FORBIDDEN_NODES: tuple[tuple[type[exp.Expression], str], ...] = (
    (exp.Into, "不允许 SELECT ... INTO（写文件或写变量）。"),
    (exp.Lock, "不允许加锁读取（FOR UPDATE / LOCK IN SHARE MODE）。"),
    (exp.SessionParameter, "不允许读取或修改系统变量（@@...）。"),
    (exp.Parameter, "不允许使用用户变量（@...）。"),
    (exp.Placeholder, "不允许使用参数占位符，请直接写入常量值。"),
    (exp.PropertyEQ, "不允许在查询中赋值（:=）。"),
)


class SqlRejected(ValueError):
    """查询未通过沙箱校验；消息直接回给模型，必须是可操作的中文提示。"""


@dataclass(frozen=True)
class PreparedQuery:
    sql: str
    scoped_tables: tuple[str, ...]


def is_queryable(table: str) -> bool:
    name = table.lower()
    return (
        name in SHARED_TABLES
        or name in SHARED_ROW_FILTERS
        or name in PERSONAL_TABLES
        or name in PERSONAL_CHILD_TABLES
    )


def is_personal(table: str) -> bool:
    name = table.lower()
    return name in PERSONAL_TABLES or name in PERSONAL_CHILD_TABLES


def scope_label(table: str) -> str | None:
    """给模型看的可见范围说明；共享表返回 None。"""
    name = table.lower()
    if not is_personal(name):
        return None
    parent = PERSONAL_CHILD_TABLES.get(name, (None, name))[1]
    if parent in PUBLIC_WHEN_UNOWNED:
        return "只返回当前用户的记录和公共记录"
    return "只返回当前用户的记录"


def prepare_query(
    sql: str,
    *,
    owner_user_id: int | None,
    max_rows: int,
    known_tables: Iterable[str] = (),
    known_routines: Iterable[str] = (),
) -> PreparedQuery:
    """校验并改写查询；任何不确定的情况都拒绝（SqlRejected），不尝试放宽。

    ``known_tables`` / ``known_routines`` 是库里真实存在的表与存储函数名，用于拦截
    与真实表同名的 CTE、以及以定义者权限执行的存储函数。
    """
    text_sql = (sql or "").strip()
    if not text_sql:
        raise SqlRejected("SQL 为空。")
    if len(text_sql) > SQL_CHAR_LIMIT:
        raise SqlRejected(f"SQL 过长（超过 {SQL_CHAR_LIMIT} 字符），请拆分查询。")
    try:
        statements = [item for item in sqlglot.parse(text_sql, read="mysql") if item is not None]
    except ParseError as exc:
        raise SqlRejected(f"SQL 解析失败：{str(exc).splitlines()[0][:300]}") from exc
    if len(statements) != 1:
        raise SqlRejected("只允许单条 SELECT 查询，不能包含多条语句。")
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.SetOperation)):
        raise SqlRejected("只允许 SELECT 查询；了解表名与字段请用 get_schema。")

    routines = {name.upper() for name in known_routines}
    for node in tree.walk():
        for node_type, message in _FORBIDDEN_NODES:
            if isinstance(node, node_type):
                raise SqlRejected(message)
        if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Func):
            raise SqlRejected("不允许调用带库名的函数（如 sys.xxx()）。")
        if isinstance(node, exp.Func):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).upper()
            if name in _FORBIDDEN_FUNCTIONS:
                raise SqlRejected(f"不允许调用函数 {name}。")
            # 存储函数以定义者权限执行内部 SQL，会绕过表级改写。
            if isinstance(node, exp.Anonymous) and name in routines:
                raise SqlRejected(f"不允许调用存储函数 {node.name}。")

    real_tables = {name.lower() for name in known_tables} | _policy_tables()
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    shadowing = sorted(cte_names & real_tables)
    if shadowing:
        raise SqlRejected(f"WITH 子句的名称不能与数据表同名：{', '.join(shadowing)}。")

    scoped: set[str] = set()
    for table in list(tree.find_all(exp.Table)):
        if table.args.get("db") or table.args.get("catalog"):
            raise SqlRejected("不允许带库名的表引用（如 information_schema.tables、其他库.表）。")
        name = table.name.lower()
        if not name:
            raise SqlRejected("不支持 JSON_TABLE 等表函数，请直接查询数据表。")
        if name in cte_names or name in SHARED_TABLES:
            continue
        if name in SHARED_ROW_FILTERS:
            table.replace(_filtered_source(table, name, SHARED_ROW_FILTERS[name]))
        elif is_personal(name):
            if owner_user_id is None:
                raise SqlRejected(f"当前会话没有用户身份，不能查询个人数据表 {name}。")
            table.replace(_filtered_source(table, name, _owner_condition(name, int(owner_user_id))))
            scoped.add(name)
        elif name == "dual":
            raise SqlRejected("MySQL 可直接 SELECT 表达式，无需 FROM DUAL。")
        else:
            raise SqlRejected(
                f"表 {table.name} 不在可查询范围（账号、密钥、日志与系统运行表不开放）。"
                "可查询的表请用 get_schema 查看。"
            )

    for hint in list(tree.find_all(exp.Hint)):
        hint.pop()
    if not tree.args.get("limit"):
        tree = tree.limit(max_rows + 1, copy=False)
    return PreparedQuery(sql=tree.sql(dialect="mysql", comments=False), scoped_tables=tuple(sorted(scoped)))


def execute_query(
    sql: str,
    *,
    owner_user_id: int | None,
    max_rows: int,
    engine: Engine | None = None,
) -> dict[str, Any]:
    """校验、改写并在只读会话中执行；拒绝时抛 SqlRejected，执行错误原样抛出。"""
    # 静态预检：绝大多数违规不需要库信息即可判定，先拒绝，免得为一条坏 SQL 连库。
    prepare_query(sql, owner_user_id=owner_user_id, max_rows=max_rows)
    target = engine or sandbox_engine()
    tables, routines = _database_objects(target)
    prepared = prepare_query(
        sql,
        owner_user_id=owner_user_id,
        max_rows=max_rows,
        known_tables=tables,
        known_routines=routines,
    )
    started = time.monotonic()
    with target.connect() as conn:
        result = conn.execution_options(no_parameters=True).exec_driver_sql(prepared.sql)
        columns = list(result.keys())
        fetched = result.fetchmany(max_rows + 1)
    return {
        "columns": columns,
        "rows": [[_serialize(value) for value in row] for row in fetched[:max_rows]],
        "row_count": min(len(fetched), max_rows),
        "truncated": len(fetched) > max_rows,
        "ms": int((time.monotonic() - started) * 1000),
        "scoped_tables": list(prepared.scoped_tables),
        "executed_sql": prepared.sql,
    }


_ENGINE: Engine | None = None
_ENGINE_LOCK = threading.Lock()
_OBJECTS_CACHE: weakref.WeakKeyDictionary[Engine, tuple[frozenset[str], frozenset[str]]] = (
    weakref.WeakKeyDictionary()
)


def sandbox_engine() -> Engine:
    """沙箱专用连接池：会话级只读 + 超时设置只作用于这些连接，不污染主连接池。"""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            engine = create_engine_for_url(settings.database_url)
            install_read_only_guard(engine)
            _ENGINE = engine
    return _ENGINE


def install_read_only_guard(engine: Engine) -> None:
    """每条新连接建立时进入只读会话；数据库层面兜底，即使校验遗漏也写不进去。"""

    @event.listens_for(engine, "connect")
    def _enter_read_only_session(dbapi_connection, _record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        try:
            if engine.dialect.name == "mysql":
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
                cursor.execute(f"SET SESSION max_execution_time = {STATEMENT_TIMEOUT_MS}")
            elif engine.dialect.name == "sqlite":
                cursor.execute("PRAGMA query_only = ON")
        finally:
            cursor.close()


def create_sandbox_engine_for_tests(url: str = "sqlite+pysqlite:///:memory:", **kwargs: Any) -> Engine:
    """测试用：带同样只读保护的非 MySQL 引擎。"""
    engine = create_engine(url, **kwargs)
    install_read_only_guard(engine)
    return engine


def _database_objects(engine: Engine) -> tuple[frozenset[str], frozenset[str]]:
    """库里真实存在的表名（含模型外的遗留表与视图）和存储函数名，进程内缓存。"""
    cached = _OBJECTS_CACHE.get(engine)
    if cached is not None:
        return cached
    try:
        inspector = inspect(engine)
        tables = {name.lower() for name in inspector.get_table_names()}
        tables |= {name.lower() for name in inspector.get_view_names()}
    except Exception:  # noqa: BLE001 - 读不到时退回模型表清单，表引用校验仍按默认拒绝
        tables = set()
    routines: set[str] = set()
    if engine.dialect.name == "mysql":
        with engine.connect() as conn:
            routines = {
                str(name).upper()
                for name in conn.exec_driver_sql(
                    "SELECT ROUTINE_NAME FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = DATABASE()"
                ).scalars()
            }
    objects = (frozenset(tables) | frozenset(Base.metadata.tables), frozenset(routines))
    _OBJECTS_CACHE[engine] = objects
    return objects


def _policy_tables() -> set[str]:
    return {*SHARED_TABLES, *SHARED_ROW_FILTERS, *PERSONAL_TABLES, *PERSONAL_CHILD_TABLES, "dual"}


def _owner_condition(table: str, owner_user_id: int) -> str:
    if table in PERSONAL_CHILD_TABLES:
        foreign_key, parent = PERSONAL_CHILD_TABLES[table]
        return f"{foreign_key} IN (SELECT id FROM {parent} WHERE {_owner_condition(parent, owner_user_id)})"
    column = PERSONAL_TABLES[table]
    if table in PUBLIC_WHEN_UNOWNED:
        return f"({column} = {owner_user_id} OR {column} IS NULL)"
    return f"{column} = {owner_user_id}"


def _filtered_source(table: exp.Table, name: str, condition: str) -> exp.Subquery:
    """把表引用换成同名（或原别名）的过滤后派生表，外层的列引用写法保持不变。"""
    inner = sqlglot.parse_one(f"SELECT * FROM {name} WHERE {condition}", read="mysql")
    alias = table.args.get("alias")
    alias_node = alias.copy() if isinstance(alias, exp.TableAlias) else exp.TableAlias(this=table.this.copy())
    return exp.Subquery(this=inner, alias=alias_node)


def _serialize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str) and len(value) > CELL_CHAR_LIMIT:
        return value[:CELL_CHAR_LIMIT] + "…（已截断）"
    return value
