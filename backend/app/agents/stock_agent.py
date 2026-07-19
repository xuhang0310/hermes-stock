from __future__ import annotations

import json
import re
import time
import threading
from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from backend.app.core.applog import log_event
from backend.app.core.database import SessionLocal, engine
from backend.app.ai.llm_client import stream_chat as _stream_chat
from backend.app.ai.tool_progress import ToolArgsProgressTracker
from backend.app.market.digital_human_tools import (
    digital_human_skill_prompt,
    digital_human_tool_display_name,
    write_tool_schemas,
)
from backend.app.core.models import LlmModelConfig
from backend.app.core.llm_config import active_llm_config
from backend.app.core.settings import settings
from backend.app.data.repository import StockRepository
from backend.app.skills.registry import skill_registry
from backend.app.agents.position_intent import (
    PositionAction,
    build_intent_hint,
    detect_position_intent,
    intent_summary,
)
from backend.app.ai.retrieval import retrieve_chunks
from backend.app.market.stock_tools import (
    stock_observation_preview,
    stock_system_prompt_with_skills,
    stock_tool_display_name,
)


AUDIT_LOG = Path(__file__).resolve().parents[2] / "agent_audit.log"  # agents/ -> app/ -> backend/
MAX_ROWS = 100
OBSERVATION_CHAR_LIMIT = 7000


def _registry_display_name(name: str) -> str:
    """从 registry 注册的工具中取展示名：stock 表 + digital_human 表兜底。"""
    return stock_tool_display_name(name)


def _registry_observation_preview(name: str, result: dict) -> str:
    """统一 preview：stock_observation_preview 覆盖了 stock + TuShare 语义封装，
    数字人工具走 digital_human_observation_preview 兜底。
    """
    if "error" in result:
        return str(result["error"])
    from backend.app.market.digital_human_tools import digital_human_observation_preview
    stock_preview = stock_observation_preview(name, result)
    if stock_preview != "工具调用完成。":
        return stock_preview
    return digital_human_observation_preview(name, result)


FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|replace|rename|"
    r"call|handler|load_file|lock|unlock|shutdown|kill|outfile|dumpfile|benchmark|sleep|"
    r"information_schema|performance_schema|mysql|"
    # 通知/调度/数字人配置表存有 webhook、飞书 App Secret 等密文，禁止 Agent 查询
    r"notify_channels|scheduled_jobs|job_runs|digital_human_profiles)\b",
    re.IGNORECASE,
)

SCHEMA_DICTIONARY: dict[str, dict[str, Any]] = {
    "instruments": {
        "label": "A股标的基础表",
        "fields": ["symbol", "name", "market", "type", "status", "list_date"],
    },
    "watchlist_items": {
        "label": "自选池成员",
        "fields": ["symbol", "group_id", "reason", "status", "added_at", "updated_at"],
    },
    "positions": {
        "label": "持仓兼容摘要表，一行代表当前系统关注的一只持仓标的",
        "fields": ["symbol", "position_pct", "cost_price", "quantity", "stop_price", "thesis", "risk_note", "status"],
    },
    "position_holding_snapshots": {
        "label": "A股券商持仓明细快照，保存截图/自然语言录入后的数量、市值、浮盈、可用数量等字段",
        "fields": [
            "symbol",
            "symbol_name",
            "as_of_date",
            "position_quantity",
            "available_quantity",
            "cost_price",
            "latest_price",
            "market_value",
            "floating_pnl",
            "floating_pnl_pct",
            "position_pct",
            "source_type",
            "captured_at",
        ],
    },
    "position_accounts": {
        "label": "持仓账户表，记录券商、账户别名、脱敏资金账号和账户类型",
        "fields": ["broker_name", "account_name", "account_no_masked", "account_type", "market", "currency", "status"],
    },
    "position_import_batches": {
        "label": "持仓 Agent 录入批次，记录自然语言或截图解析过程、模型返回和入库状态",
        "fields": ["source_type", "source_name", "status", "image_sha256", "raw_text", "parsed_json", "created_by", "created_at"],
    },
    "events": {
        "label": "规则与市场事件",
        "fields": ["event_id", "level", "event_type", "symbol", "title", "summary", "facts_json", "status", "created_at"],
    },
    "agent_reports": {
        "label": "Agent研究/风控报告",
        "fields": ["report_id", "agent_name", "symbol", "title", "conclusion", "risk_points", "action_suggestion", "created_at"],
    },
    "risk_checks": {
        "label": "风控检查记录",
        "fields": ["symbol", "risk_level", "risk_type", "finding", "required_action", "cool_down_required", "created_at"],
    },
    "system_logs": {
        "label": "系统运行日志",
        "fields": ["level", "module", "message", "context_json", "created_at"],
    },
    # 知识库表（检索 agent 可探索）
    "documents": {
        "label": "知识库文档表，记录所有已入库的文档元信息",
        "fields": ["doc_type", "title", "source", "trust_level", "published_at", "fetched_at"],
    },
    "document_chunks": {
        "label": "知识库文档切块表，文档正文经切分后的最小检索单元",
        "fields": ["doc_id", "chunk_index", "content", "token_count", "status", "content_hash"],
    },
    "document_symbols": {
        "label": "文档-标的关联表（多对多）",
        "fields": ["doc_id", "symbol", "relevance"],
    },
    "knowledge_uploads": {
        "label": "知识库上传批次表，记录文件上传与解析状态",
        "fields": ["filename", "mime_type", "size", "doc_id", "status"],
    },
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": "在只读模式下执行一条 MySQL SELECT 查询并返回最多100行结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "单条 SELECT 语句，不要带分号，不要使用写入语句。"}
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_schema",
            "description": "查看 A 股研究系统的表结构。不传 table_name 返回所有可用表名清单（含业务标签）；传 table_name 返回该表的业务含义和常用字段。探索数据库结构请用本工具，不要用 run_sql 查 SHOW TABLES 或 information_schema（被沙箱禁止）。",
            "parameters": {
                "type": "object",
                "properties": {"table_name": {"type": "string", "description": "可选表名，如 positions。留空返回所有表名清单。"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "在知识库中执行混合检索（向量+关键词召回→RRF融合→重排精排），返回最相关的文档片段。可用 top_k 控制返回条数，用 symbol 限定关联标的，用 doc_types 限定文档类型（news/report/notice/research/upload），用 trust_levels 限定可信度。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索问句，如'新能源政策''芯片行业研报'"},
                    "top_k": {"type": "integer", "description": "返回条数，默认6，上限20"},
                    "symbol": {"type": "string", "description": "可选，限定A股标的代码，如600519.SH"},
                    "doc_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选，限定文档类型列表，如['research', 'news']",
                    },
                    "trust_levels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选，限定可信度列表，如['trusted', 'normal']",
                    },
                },
                "required": ["query"],
            },
        },
    },
]


def _knowledge_system_prompt(memory_context: str = "") -> str:
    """System prompt for chat_mode='knowledge': 知识库检索专家，核心工具 search_knowledge。"""
    schema_lines = [
        f"- {table}: {meta['label']}，常用字段 {', '.join(meta['fields'])}"
        for table, meta in SCHEMA_DICTIONARY.items()
    ]
    memory_block = f"\n\n{memory_context.strip()}" if memory_context.strip() else ""
    return f"""你是 SixRabbits Stock 的知识库检索专家，采用 ReAct 方式工作。
今天日期: {date.today().isoformat()}

可查询的数据表（含知识库表）:
{chr(10).join(schema_lines)}

{memory_block}

工作规则:
1. 核心工具是 search_knowledge —— 在已索引的文档切块中做混合检索（向量+关键词召回→RRF融合→重排精排），支持按标的(symbol)、文档类型(doc_types)、可信度(trust_levels)过滤。
2. search_knowledge 返回的每条结果包含 doc_type（news/report/notice/research/upload）、title、source、content 和四路评分（vector/keyword/fused/rerank），要引用相关片段作答，标注来源。
3. 必要时用 get_schema 探索数据库中的实体关系（如 documents 与 document_chunks 的关联），再用 run_sql 精确查询聚合统计或关联记录。
4. run_sql 只能执行 SELECT 只读查询，不允许写入、DDL、系统库查询。
5. SQL 查询前先用一句短中文说明要查什么；查询后根据 observation 校验，再继续下一步。
6. 所有结论必须标注来源（文档标题、类型、可信度）；不得编造信息。
7. 对于简单的招呼、闲聊或与知识库搜索无关的话题，简短友好地直接回应，不查数据、不调用工具。
8. 最终回答使用中文 Markdown，条理清晰、引用准确。"""


def _system_prompt(extra_context: str | None = None, digital_human_code: str | None = None, memory_context: str = "") -> str:
    schema_lines = [
        f"- {table}: {meta['label']}，常用字段 {', '.join(meta['fields'])}"
        for table, meta in SCHEMA_DICTIONARY.items()
    ]
    context = f"\n\n当前数字人职责:\n{extra_context.strip()}" if extra_context and extra_context.strip() else ""
    memory_block = f"\n\n{memory_context.strip()}" if memory_context.strip() else ""
    skill_block = digital_human_skill_prompt(digital_human_code)
    # 技能补充规则：基础提示词之外，由 registry 汇总各激活技能声明的行为规范（如工作区防注入）。
    # 与工具装配同源过滤（chat_mode+role），保证"给了工具就给对应规则"。基础提示词已覆盖的
    # 技能其 prompt_instructions 留空，不会重复注入。
    extra_rules = skill_registry.get_prompt_instructions(chat_mode="research", role_code=digital_human_code)
    skill_rules_block = ("\n\n技能补充规则:\n" + "\n".join(f"- {r}" for r in extra_rules)) if extra_rules else ""
    return f"""你是 SixRabbits Stock A股多智能体研究台的数字人——既是有温度、会聊天的人，也是严谨的A股数据研究专家，采用 ReAct 方式工作。
今天日期: {date.today().isoformat()}

可查询的数据表:
{chr(10).join(schema_lines)}

{skill_block}
{memory_block}
对话原则（每次先判断）:
- 遇到寒暄、闲聊、情绪倾诉、生活话题或与股票/研究无关的问题：就像朋友一样自然、亲切地直接回应，不查数据、不套研究框架、不调用任何工具，保持你的口吻，简短有人情味，可以适度共情、开个得体的玩笑。
- 只有遇到持仓、行情、事件、自选、研究、风控等专业问题时，才切到专业模式，按下面的工作规则用工具和证据严谨作答。
- 拿不准是闲聊还是提问时：先温和回应，再问一句对方想了解什么。始终做“懂行又好相处的朋友”，不是冷冰冰的查询终端。

工作规则（仅适用于专业研究问题）:
1. 默认研究范围是系统自己的 open positions 和 active watchlist_items；问题未指定标的时，先分析持仓，再分析自选。用户明确问其它标的、行业或话题时，直接就其问题作答，不必强行绕回持仓。
2. 【必做】凡回答涉及具体股票的行情、判断、风控问题，先调用 get_latest_price 获取该标的最新价格（数据库缓存，开盘时段自动刷新、收盘后走缓存不外呼），回答中标注价格与所属交易日；绝不引用记忆里的旧价。
3. 优先使用 get_my_universe / get_portfolio_snapshot / get_watchlist_snapshot / get_symbol_context 等结构化工具。
4. 探索数据库结构用 get_schema（不传参列所有表名，再传表名查字段）；run_sql 是兜底工具，只能对已知表做 SELECT 查询，禁止查 SHOW TABLES / information_schema / 系统库（会被沙箱拒绝）。
5. 系统内没有的信息——外部个股、行业、政策、新闻、宏观、突发事件等——用 search_market_intel / search_stock_news 联网搜索补充，再结合内部数据作答；不要以"不在持仓/自选池"为由拒绝，你本职专业之外但用户确实需要的问题也尽力帮忙。
6. SQL 只能执行 SELECT 只读查询，不允许写入、DDL、系统库查询或多语句。
7. 查询前先用一句短中文说明要查什么；查询后根据 observation 校验，再继续下一步。
8. 所有结论必须标注数据来源或说明样例/缺失；不得编造行情、新闻或财务数据。
9. A股输出要区分事实、推断、风险、下一步动作，不提供自动下单建议，不构成投资建议。
10. 最终回答使用中文 Markdown，尽量短、清楚、可执行。{skill_rules_block}{context}"""


def validate_sql(sql: str) -> str | None:
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        return "SQL 为空。"
    if ";" in stripped:
        return "只允许单条 SQL，不能包含分号。"
    if "--" in stripped or "#" in stripped or "/*" in stripped:
        return "不允许包含 SQL 注释。"
    if not stripped.lower().startswith("select"):
        return "只允许 SELECT 查询；如需了解表结构或表名清单请用 get_schema 工具（不传参可列所有表）。"
    match = FORBIDDEN_SQL.search(stripped)
    if match:
        return f"包含被禁止的关键字: {match.group(0)}；如需了解表结构请用 get_schema 工具。"
    return None


def _normalize_sql(sql: str) -> str:
    stripped = sql.strip().rstrip(";").strip()
    if not re.search(r"\blimit\s+\d+", stripped, re.IGNORECASE):
        stripped = f"{stripped} LIMIT {MAX_ROWS}"
    return stripped


def _serialize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _audit(record: dict) -> None:
    try:
        record["at"] = datetime.now().isoformat(timespec="seconds")
        with AUDIT_LOG.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass
    try:  # 同时并入统一日志文件（agent_sql 高频，仅落文件不入库）
        from backend.app.core.applog import get_logger

        summary = {k: record.get(k) for k in ("tool", "rows", "ms", "error", "rejected") if record.get(k) is not None}
        get_logger("agent_sql").info(json.dumps(summary, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass


def run_sql(sql: str) -> dict:
    error = validate_sql(sql)
    if error:
        _audit({"tool": "run_sql", "sql": sql, "rejected": error})
        return {"error": error}
    normalized = _normalize_sql(sql)
    started = time.monotonic()
    try:
        with engine.connect() as conn:
            result = conn.execute(text(normalized))
            columns = list(result.keys())
            rows = [[_serialize(value) for value in row] for row in result.fetchmany(MAX_ROWS)]
            truncated = result.fetchone() is not None
    except Exception as exc:  # noqa: BLE001
        _audit({"tool": "run_sql", "sql": normalized, "error": str(exc)[:500]})
        return {"error": f"SQL 执行失败: {str(exc)[:500]}"}
    elapsed_ms = int((time.monotonic() - started) * 1000)
    _audit({"tool": "run_sql", "sql": normalized, "rows": len(rows), "ms": elapsed_ms})
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated, "ms": elapsed_ms}


# 表名本身非密文，但这几张表存有 webhook / 飞书 App Secret 等，不在 get_schema 列表里暴露
_SCHEMA_HIDDEN_TABLES: frozenset[str] = frozenset({
    "notify_channels", "scheduled_jobs", "job_runs", "digital_human_profiles",
})


def get_schema(table_name: str = "") -> dict:
    """查看表结构。不传 table_name 返回所有可用表名清单（过滤密文表）；
    传 table_name 返回该表的业务含义与常用字段。让 Agent 用此工具探索 schema，
    不要用 run_sql 查 SHOW TABLES / information_schema（被只读沙箱禁止）。
    """
    key = (table_name or "").strip()
    if not key:
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = DATABASE() ORDER BY table_name"
                    )
                ).scalars().all()
        except Exception as exc:  # noqa: BLE001
            return {"error": f"读取表清单失败: {str(exc)[:200]}"}
        tables = [
            {
                "name": t,
                "label": SCHEMA_DICTIONARY.get(t, {}).get("label", "未文档化"),
                "documented": t in SCHEMA_DICTIONARY,
            }
            for t in rows
            if t not in _SCHEMA_HIDDEN_TABLES
        ]
        return {
            "tables": tables,
            "count": len(tables),
            "hint": "传 table_name 查某张表的字段含义；未文档化的表可用 run_sql 做 SELECT 查询。",
        }
    meta = SCHEMA_DICTIONARY.get(key)
    if not meta:
        return {
            "error": (
                f"表 {key} 未在字段字典中。可：1) get_schema() 不传参查看所有表名；"
                f"2) 该表若确实存在，用 run_sql 做 SELECT 查询（仅 SELECT）。"
            )
        }
    return {"table": key, **meta}


def search_knowledge(
    query: str,
    top_k: int | None = None,
    symbol: str | None = None,
    doc_types: list[str] | None = None,
    trust_levels: list[str] | None = None,
) -> dict:
    """搜索知识库，返回带四路分数的命中切块列表。"""
    if not query.strip():
        return {"error": "搜索问句不能为空"}
    with SessionLocal() as db:
        try:
            hits = retrieve_chunks(
                db,
                query.strip(),
                top_k=top_k or None,
                symbol=symbol,
                doc_types=doc_types,
                trust_levels=trust_levels,
            )
        except Exception as exc:
            return {"error": f"知识库检索失败: {str(exc)[:300]}"}
    return {"query": query, "hits": hits, "hit_count": len(hits)}


def _knowledge_search_preview(result: dict) -> str:
    """格式化 search_knowledge 结果为 observation 预览文本。"""
    if "error" in result:
        return str(result["error"])
    count = result.get("hit_count", 0)
    if not count:
        return "无匹配结果"
    preview_parts = [f"知识库检索返回 {count} 条结果"]
    for i, hit in enumerate(result.get("hits", [])[:3]):
        title = hit.get("title", "")
        content_snippet = hit.get("content", "")[:120]
        preview_parts.append(f"#{i+1} [{hit.get('doc_type','')}] {title}: {content_snippet}...")
    return "\n".join(preview_parts)


_TOOL_MAX_RETRY = 2  # 同一工具连续失败到此次数后，改为"停止重试、用现有信息作答"引导


def _tool_retry_hint(name: str, failures: int) -> str:
    """工具失败时附加到 observation 的结构化引导（借鉴 deepresearch 反思重试，单 agent 轻量版）。

    未到上限：引导修正后重试一次；到上限：明确要求停止调用该工具、用已有信息收口，
    避免把有限的 ReAct 步数烧在反复试错上。
    """
    if failures >= _TOOL_MAX_RETRY:
        return (
            f"\n\n[系统提示] 工具 {name} 已连续失败 {failures} 次，请不要再调用它。"
            "改用已经获得的信息尽力作答，或如实告诉用户此项数据/操作暂不可用，不要继续空转。"
        )
    return (
        f"\n\n[系统提示] 工具 {name} 本次执行失败（第 {failures} 次）。"
        "请依据上面的错误信息修正参数后重试一次；若仍不行，改用其他工具或如实告知用户，不要直接放弃。"
    )


def _observation_preview(result: dict) -> str:
    if "error" in result:
        return result["error"]
    parts = [f"返回 {result['row_count']} 行（{result['ms']}ms）"]
    if result.get("truncated"):
        parts.append(f"已截断至 {MAX_ROWS} 行")
    if result.get("rows"):
        sample = dict(zip(result["columns"], result["rows"][0]))
        parts.append("示例: " + json.dumps(sample, ensure_ascii=False)[:400])
    return "；".join(parts)


def _execute_position_write_tool(
    name: str,
    args: dict[str, Any],
    *,
    user_id: int,
    user_label: str,
    default_account_id: int | None,
    digital_human_code: str | None,
) -> dict:
    """数字人写持仓工具：经独立 repository 会话复用 import 链写入（快照式），全程审计。

    不经 execute_digital_human_tool/registry（它们只传 name/args，不带用户/账户/db 上下文）。
    run_agent_events 跑在后台线程，此处用自己的 SessionLocal，不复用请求 db（线程安全）。
    """
    if name == "update_position_field":
        return _do_update_position_field(args, user_id=user_id, default_account_id=default_account_id, digital_human_code=digital_human_code)
    if name == "rollback_last_position_batch":
        return _do_rollback_last_batch(args, user_id=user_id, digital_human_code=digital_human_code)
    if name != "update_my_positions":
        return {"error": f"未知写工具: {name}"}
    rows = args.get("rows") if isinstance(args.get("rows"), list) else []
    if not rows:
        return {"error": "没有可保存的持仓明细 rows"}
    account = args.get("account") if isinstance(args.get("account"), dict) else {}
    default_account: dict[str, Any] = {**account}
    if default_account_id and "account_id" not in default_account:
        # 弱提示键：repository 先按"该标的现有 open 持仓的账户"自动匹配（清仓/卖出必须命中原持仓），
        # 匹配不到才落这个默认账户；直接塞 account_id 会把更新写到别的账户、真实持仓纹丝不动。
        default_account["default_account_id"] = default_account_id
    if args.get("as_of_date"):
        default_account["as_of_date"] = args.get("as_of_date")
    # P2 写前确认（人在环）：confirmed 留空/false 只返回预览，不落库；用户确认后传 true 才写
    if not bool(args.get("confirmed")):
        return {
            "preview": True,
            "row_count": len(rows),
            "account": default_account,
            "rows_preview": rows[:3],
            "hint": "预览模式未落库。复述给用户确认后，再以 confirmed=true 调用才真正写入。",
        }
    source_name = f"digital_human:{digital_human_code or 'unknown'}"
    started = time.monotonic()
    try:
        with SessionLocal() as session:
            repository = StockRepository(session)
            batch = repository.create_position_import_batch(
                source_name=source_name,
                image_mime_type="",
                image_sha256=None,
                raw_text=json.dumps(args, ensure_ascii=False),
                parsed_json=json.dumps(args, ensure_ascii=False),
                created_by=user_label,
            )
            saved_rows = repository.import_position_rows(rows, batch, default_account)
            repository.finish_position_import_batch(batch, saved_rows)
            session.commit()
    except Exception as exc:  # noqa: BLE001
        log_event("position", "ERROR", "数字人写持仓失败", user_id=user_id, error=str(exc)[:300])
        return {"error": f"写持仓失败: {str(exc)[:300]}"}
    ms = int((time.monotonic() - started) * 1000)
    ok_rows = [row for row in saved_rows if "error" not in row]
    err_rows = [row for row in saved_rows if "error" in row]
    log_event(
        "position", "INFO", "数字人写持仓",
        user_id=user_id, batch_id=batch.id, saved=len(ok_rows), errors=len(err_rows), ms=ms,
    )
    return {
        "batch_id": batch.id,
        "saved_count": len(ok_rows),
        "error_count": len(err_rows),
        "rows": saved_rows,
        "ms": ms,
        "source": source_name,
    }


def _do_update_position_field(args: dict[str, Any], *, user_id: int, default_account_id: int | None, digital_human_code: str | None) -> dict:
    """字段级微调：复用 repository.update_position_field，独立 session，审计。"""
    symbol = str(args.get("symbol") or "")
    field = str(args.get("field") or "")
    value = args.get("value")
    if not symbol or not field:
        return {"error": "symbol 和 field 必填"}
    if not bool(args.get("confirmed")):
        return {"preview": True, "symbol": symbol, "field": field, "value": value, "hint": "预览未落库，confirmed=true 才改"}
    started = time.monotonic()
    try:
        with SessionLocal() as session:
            repository = StockRepository(session)
            result = repository.update_position_field(symbol, field, value, account_id=default_account_id)
            session.commit()
    except Exception as exc:  # noqa: BLE001
        log_event("position", "ERROR", "数字人改字段失败", user_id=user_id, error=str(exc)[:300])
        return {"error": f"改字段失败: {str(exc)[:300]}"}
    ms = int((time.monotonic() - started) * 1000)
    if "error" not in result:
        log_event("position", "INFO", "数字人改持仓字段", user_id=user_id, symbol=symbol, field=field, ms=ms)
        result["ms"] = ms
    return result


def _do_rollback_last_batch(args: dict[str, Any], *, user_id: int, digital_human_code: str | None) -> dict:
    """撤销最近一次持仓导入批次：复用 repository.rollback_last_position_batch，独立 session，审计。"""
    if not bool(args.get("confirmed")):
        return {"preview": True, "hint": "将撤销最近一次导入批次的快照，confirmed=true 才执行"}
    started = time.monotonic()
    try:
        with SessionLocal() as session:
            repository = StockRepository(session)
            result = repository.rollback_last_position_batch()
            session.commit()
    except Exception as exc:  # noqa: BLE001
        log_event("position", "ERROR", "数字人撤销批次失败", user_id=user_id, error=str(exc)[:300])
        return {"error": f"撤销失败: {str(exc)[:300]}"}
    ms = int((time.monotonic() - started) * 1000)
    if "error" not in result:
        log_event("position", "INFO", "数字人撤销持仓批次", user_id=user_id, ms=ms)
        result["ms"] = ms
    return result


def _position_write_preview(name: str, result: dict) -> str:
    if "error" in result:
        return str(result["error"])
    if result.get("preview"):
        return f"预览（未落库）：{result.get('hint', '')}"
    if name == "update_my_positions":
        return (
            f"已更新持仓：成功 {result.get('saved_count', 0)} 条，失败 {result.get('error_count', 0)} 条"
            f"（batch #{result.get('batch_id', '-')}, {result.get('ms', '-')}ms）"
        )
    if name == "update_position_field":
        return f"已改 {result.get('symbol', '-')} 的 {result.get('field', '-')} = {result.get('value', '-')}（快照 #{result.get('snapshot_id', '-')})"
    if name == "rollback_last_position_batch":
        return f"已撤销批次 #{result.get('batch_id', '-')}, 删 {result.get('rolled_back_count', 0)} 条快照"
    return "写持仓完成"


def run_agent_events(
    question: str,
    history: list[dict],
    digital_human_context: str | None = None,
    model_config_id: int | None = None,
    chat_mode: str = "research",
    skills: list[str] | None = None,
    digital_human_code: str | None = None,
    memory_context: str = "",
    user_id: int | None = None,
    user_label: str | None = None,
    can_write_positions: bool = False,
    default_account_id: int | None = None,
    cancel_event: threading.Event | None = None,
) -> Generator[dict, None, None]:
    stock_mode = chat_mode == "stock"
    knowledge_mode = chat_mode == "knowledge"
    digital_tool_names: set[str] = set()
    write_tool_names: set[str] = set()
    if stock_mode:
        tools = skill_registry.get_tools_for("stock")
    elif knowledge_mode:
        tools = list(TOOLS)  # knowledge mode 仍走硬编码：tools 固定且不参与角色筛选
    else:
        # research mode
        tools = [*TOOLS, *skill_registry.get_tools_for("research", role_code=digital_human_code)]
        if can_write_positions:
            w_schemas = write_tool_schemas()
            if w_schemas:
                tools = [*tools, *w_schemas]
                write_tool_names = {s["function"]["name"] for s in w_schemas}
    system_prompt = (
        stock_system_prompt_with_skills(digital_human_context, skills, memory_context)
        if stock_mode
        else _knowledge_system_prompt(memory_context)
        if knowledge_mode
        else _system_prompt(digital_human_context, digital_human_code, memory_context)
    )
    # P1: 意图预处理 — 在 LLM 调用前检测交易动作，注入 system prompt（仅 research 模式）
    intent = detect_position_intent(question) if not stock_mode and not knowledge_mode and can_write_positions else None
    intent_hint = (
        build_intent_hint(intent)
        if intent and intent.action != PositionAction.UNKNOWN and intent.confidence >= 0.7
        else ""
    )
    if intent_hint:
        system_prompt += intent_hint
    # stock 模式不做持仓写入，不注入交易意图提示
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    for item in (history or [])[-8:]:
        if item.get("role") in {"user", "assistant"} and item.get("content"):
            messages.append({"role": item["role"], "content": item["content"]})
    messages.append({"role": "user", "content": question})

    llm_config = active_llm_config(model_config_id)
    yield {
        "type": "model_selected",
        "model": {
            "id": llm_config.get("id"),
            "display_name": llm_config.get("display_name"),
            "model_name": llm_config.get("model"),
            "provider_name": llm_config.get("provider_name"),
        },
    }
    # 向前端推送识别到的交易意图（仅 research 模式 + 写权限开启时）
    if intent and intent.action != PositionAction.UNKNOWN:
        yield {"type": "position_intent", **intent_summary(intent)}

    run_started = time.monotonic()
    completed_tool_label: str | None = None
    tool_failures: dict[str, int] = {}  # 工具连续失败计数（整个 run 内累计），驱动结构化重试引导
    if stock_mode or knowledge_mode:
        yield {"type": "stage_start", "stage": "agent_loop", "message": "Starting agent analysis..."}
    for _ in range(llm_config["max_steps"]):
        if cancel_event and cancel_event.is_set():
            return
        message: dict | None = None
        args_tracker = ToolArgsProgressTracker()  # 每轮新建：工具参数生成期（写文件等长参数）的进度可视化
        if stock_mode or knowledge_mode:
            if completed_tool_label:
                yield {"type": "thinking", "message": f"「{completed_tool_label}」已完成，继续深入分析..."}
                completed_tool_label = None
            else:
                yield {"type": "thinking", "message": "正在制定分析路径..."}
        try:
            for kind, payload in _stream_chat(messages, llm_config, tools):
                if kind == "reasoning":
                    yield {"type": "reasoning_delta", "content": payload}
                elif kind == "content":
                    yield {"type": "content_delta", "content": payload}
                elif kind == "tool_args":
                    progress = args_tracker.feed(payload["index"], payload["name"], payload["delta"])
                    if progress:
                        progress["display_name"] = digital_human_tool_display_name(progress["tool"]) if progress["tool"] else ""
                        yield progress
                else:
                    message = payload
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "content": str(exc)}
            return

        if message is None:
            yield {"type": "error", "content": "模型流式响应异常，请重试。"}
            return

        tool_calls = message.get("tool_calls") or []
        content = (message.get("content") or "").strip()
        if not tool_calls:
            if not content:
                yield {"type": "error", "content": "模型未返回内容，请重试。"}
                return
            if stock_mode or knowledge_mode:
                yield {"type": "generating", "message": "正在生成最终分析..."}
            yield {"type": "content_end", "role": "answer"}
            if stock_mode or knowledge_mode:
                yield {
                    "type": "stage_done",
                    "stage": "agent_loop",
                    "status": "completed",
                    "duration": round(time.monotonic() - run_started, 2),
                }
            return

        yield {"type": "content_end", "role": "thought"}
        messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": tool_calls})

        for call in tool_calls:
            if cancel_event and cancel_event.is_set():
                return
            name = call.get("function", {}).get("name", "")
            try:
                args = json.loads(call.get("function", {}).get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}

            if name in write_tool_names:
                # 写持仓工具需要额外上下文，不经过 registry
                display_name = digital_human_tool_display_name(name)
                yield {"type": "action", "tool": name, "display_name": display_name, "content": json.dumps(args, ensure_ascii=False)}
                result = _execute_position_write_tool(
                    name, args,
                    user_id=user_id or 0,
                    user_label=user_label or "unknown",
                    default_account_id=default_account_id,
                    digital_human_code=digital_human_code,
                )
                yield {"type": "observation", "content": _position_write_preview(name, result)}
            elif name == "search_knowledge":
                query = str(args.get("query", ""))
                top_k = args.get("top_k")
                symbol = str(args.get("symbol") or "")
                doc_types = args.get("doc_types") if isinstance(args.get("doc_types"), list) else None
                trust_levels = args.get("trust_levels") if isinstance(args.get("trust_levels"), list) else None
                display = f"搜索知识库: {query[:80]}"
                yield {"type": "action", "tool": "search_knowledge", "display_name": "知识库检索", "content": display}
                result = search_knowledge(query, top_k=top_k, symbol=symbol or None, doc_types=doc_types, trust_levels=trust_levels)
                yield {"type": "observation", "content": _knowledge_search_preview(result)}
            elif name == "run_sql":
                sql = str(args.get("sql", ""))
                yield {"type": "action", "tool": "run_sql", "content": sql}
                result = run_sql(sql)
                yield {"type": "observation", "content": _observation_preview(result)}
            elif name == "get_schema":
                table = str(args.get("table_name") or "")
                yield {"type": "action", "tool": "get_schema", "content": table or "(全部表清单)"}
                result = get_schema(table)
                if "error" in result:
                    yield {"type": "observation", "content": result["error"]}
                else:
                    label = f"{table} 的数据字典" if table else f"所有表清单（{result.get('count', 0)} 张）"
                    yield {"type": "observation", "content": f"已返回 {label}"}
            elif skill_registry.has_tool(name):
                display_name = _registry_display_name(name)
                if stock_mode:
                    yield {"type": "tool_start", "tool": name, "display_name": display_name}
                yield {"type": "action", "tool": name, "content": json.dumps(args, ensure_ascii=False)}
                tool_started = time.monotonic()
                if name.startswith("ws_"):
                    from backend.app.skills.agent_workspace import workspace_user_scope

                    with workspace_user_scope(user_id):
                        result = skill_registry.execute(name, args)
                else:
                    result = skill_registry.execute(name, args)
                duration = round(time.monotonic() - tool_started, 2)
                yield {"type": "observation", "content": _registry_observation_preview(name, result)}
                if stock_mode:
                    yield {
                        "type": "tool_done",
                        "tool": name,
                        "display_name": display_name,
                        "success": "error" not in result,
                        "duration": duration,
                    }
                    completed_tool_label = display_name
            else:
                result = {"error": f"未知工具 {name}"}
                yield {"type": "observation", "content": result["error"]}

            tool_content = json.dumps(result, ensure_ascii=False)[:OBSERVATION_CHAR_LIMIT]
            if isinstance(result, dict) and "error" in result:
                # 失败：在塞回模型的 observation 后附结构化引导，治"一次错就放弃"与"反复空转"两头
                tool_failures[name] = tool_failures.get(name, 0) + 1
                tool_content += _tool_retry_hint(name, tool_failures[name])
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": tool_content,
                }
            )

    yield {"type": "error", "content": f"已达到最大 ReAct 步数（{llm_config['max_steps']}），请把问题拆小后重试。"}
