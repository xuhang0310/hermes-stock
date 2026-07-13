from __future__ import annotations

import json
import re
import time
from collections.abc import Generator, Iterable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from backend.app.core.applog import log_event
from backend.app.core.database import SessionLocal, engine
from backend.app.market.digital_human_tools import (
    digital_human_observation_preview,
    digital_human_skill_prompt,
    digital_human_tool_display_name,
    digital_human_tool_names,
    digital_human_tool_schemas,
    execute_digital_human_tool,
    write_tool_schemas,
)
from backend.app.core.models import LlmModelConfig
from backend.app.core.settings import settings
from backend.app.data.repository import StockRepository
from backend.app.market.stock_tools import (
    STOCK_TOOL_SCHEMAS,
    execute_stock_tool,
    stock_tool_display_name,
    stock_observation_preview,
    stock_system_prompt_with_skills,
)


AUDIT_LOG = Path(__file__).resolve().parents[2] / "agent_audit.log"  # agents/ -> app/ -> backend/
MAX_ROWS = 100
OBSERVATION_CHAR_LIMIT = 7000

# stock 模式工具事件分发白名单：run_agent_events 仅对集合内工具 yield tool_start/tool_done/action，
# 不在集合的工具落到 else 分支返回"未知工具"。新增 stock 工具必须同步加此常量、STOCK_TOOL_SCHEMAS、
# skills 模块 executor 三处（三源一致）。Phase 2 切 registry 接管分发后可改为从 schema 自动抽取。
ALLOWED_STOCK_TOOLS: frozenset[str] = frozenset({
    "resolve_stock",
    "get_tushare_daily",
    "get_daily_history",
    "get_realtime_quote",
    "analyze_daily_bars",
    "analyze_trend",
    "calculate_ma",
    "get_volume_analysis",
    "get_chip_distribution",
    "get_capital_flow",
    "get_stock_info",
    "get_analysis_context",
    "analyze_pattern",
    "search_stock_news",
    "search_market_intel",
    "search_comprehensive_intel",
    # TuShare 全量接口落地：通用透传 + 目录查询 + 首批语义封装（Phase 0.5 + 1a）
    "tushare_query",
    "list_tushare_apis",
    "get_trade_cal",
    "get_suspend",
    "get_stk_limit",
    "get_top_list",
    "get_hm_detail",
    "get_margin",
    "get_moneyflow_hsgt",
    "get_forecast",
    "get_income",
    "get_index_daily",
    "get_sw_daily",
    "get_cn_cpi",
    "get_tushare_minutes",
    # 二批语义封装（Phase 1b）
    "get_top_inst",
    "get_limit_list",
    "get_limit_step",
    "get_margin_detail",
    "get_share_float",
    "get_block_trade",
    "get_top10_holders",
    "get_top10_float_holders",
    "get_holdertrade",
    "get_holdernumber",
    "get_repurchase",
    "get_pledge_stat",
    "get_express",
    "get_dividend",
    "get_balancesheet",
    "get_cashflow",
    "get_fina_mainbz",
    "get_disclosure_date",
    "get_index_weight",
    "get_index_member",
    "get_daily_info",
    "get_report_rc",
    "get_anns",
    "get_namechange",
    "get_stk_factor",
    "get_ccass_hold",
})

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
]


def _system_prompt(extra_context: str | None = None, digital_human_code: str | None = None, memory_context: str = "") -> str:
    schema_lines = [
        f"- {table}: {meta['label']}，常用字段 {', '.join(meta['fields'])}"
        for table, meta in SCHEMA_DICTIONARY.items()
    ]
    context = f"\n\n当前数字人职责:\n{extra_context.strip()}" if extra_context and extra_context.strip() else ""
    memory_block = f"\n\n{memory_context.strip()}" if memory_context.strip() else ""
    skill_block = digital_human_skill_prompt(digital_human_code)
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
2. 优先使用 get_my_universe / get_portfolio_snapshot / get_watchlist_snapshot / get_symbol_context 等结构化工具。
3. 探索数据库结构用 get_schema（不传参列所有表名，再传表名查字段）；run_sql 是兜底工具，只能对已知表做 SELECT 查询，禁止查 SHOW TABLES / information_schema / 系统库（会被沙箱拒绝）。
4. 系统内没有的信息——外部个股、行业、政策、新闻、宏观、突发事件等——用 search_market_intel / search_stock_news 联网搜索补充，再结合内部数据作答；不要以"不在持仓/自选池"为由拒绝，你本职专业之外但用户确实需要的问题也尽力帮忙。
5. SQL 只能执行 SELECT 只读查询，不允许写入、DDL、系统库查询或多语句。
6. 查询前先用一句短中文说明要查什么；查询后根据 observation 校验，再继续下一步。
7. 所有结论必须标注数据来源或说明样例/缺失；不得编造行情、新闻或财务数据。
8. A股输出要区分事实、推断、风险、下一步动作，不提供自动下单建议，不构成投资建议。
9. 最终回答使用中文 Markdown，尽量短、清楚、可执行。{context}"""


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


def _active_llm_config(model_config_id: int | None = None) -> dict:
    fallback = {
        "id": None,
        "display_name": "环境变量默认模型",
        "provider_name": "环境变量",
        "base_url": settings.llm_base_url,
        "api_key": settings.llm_api_key,
        "model": settings.llm_model,
        "temperature": 0.2,
        "max_steps": settings.agent_max_steps,
    }
    with SessionLocal() as session:
        query = select(LlmModelConfig).options(selectinload(LlmModelConfig.platform))
        if model_config_id:
            row = session.scalar(query.where(LlmModelConfig.id == model_config_id, LlmModelConfig.status != "disabled").limit(1))
            if row is None:
                raise RuntimeError("所选模型配置不存在或已停用。")
        else:
            row = session.scalar(
                query.where(LlmModelConfig.is_active.is_(True), LlmModelConfig.status == "enabled")
                .order_by(LlmModelConfig.updated_at.desc(), LlmModelConfig.id.desc())
                .limit(1)
            )
            if row is None:
                return fallback

        platform = row.platform
        return {
            "id": row.id,
            "display_name": row.display_name,
            "provider_name": platform.name if platform else row.provider_name,
            "base_url": (platform.base_url if platform else row.base_url) or fallback["base_url"],
            "api_key": (platform.api_key if platform and platform.api_key else row.api_key) or fallback["api_key"],
            "model": row.model_name or fallback["model"],
            "temperature": float(row.temperature if row.temperature is not None else fallback["temperature"]),
            "max_steps": int(row.max_steps or fallback["max_steps"]),
        }


def _parse_sse_events(lines: Iterable[str]) -> Generator[tuple[str, Any], None, None]:
    content_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    for line in lines:
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning:
            yield "reasoning", reasoning
        text_piece = delta.get("content")
        if text_piece:
            content_parts.append(text_piece)
            yield "content", text_piece
        for item in delta.get("tool_calls") or []:
            slot = tool_calls.setdefault(int(item.get("index", 0)), {"id": "", "function": {"name": "", "arguments": ""}})
            if item.get("id"):
                slot["id"] = item["id"]
            function = item.get("function") or {}
            if function.get("name"):
                slot["function"]["name"] = function["name"]
            if function.get("arguments"):
                slot["function"]["arguments"] += function["arguments"]

    message: dict = {"content": "".join(content_parts)}
    if tool_calls:
        message["tool_calls"] = [
            {"id": slot["id"] or f"call_{index}", "type": "function", "function": slot["function"]}
            for index, slot in sorted(tool_calls.items())
        ]
    yield "message", message


def _stream_chat(messages: list[dict], llm_config: dict, tools: list[dict] | None = None) -> Generator[tuple[str, Any], None, None]:
    if not llm_config["api_key"] or not llm_config["base_url"]:
        raise RuntimeError("当前模型未配置 API Key 或 Base URL，请先在模型平台/模型配置中补齐。")
    url = llm_config["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": llm_config["model"],
        "messages": messages,
        "tools": tools or TOOLS,
        "temperature": llm_config["temperature"],
        "stream": True,
    }
    headers = {"Authorization": f"Bearer {llm_config['api_key']}"}
    with httpx.Client(timeout=httpx.Timeout(180.0, connect=15.0)) as client:
        with client.stream("POST", url, json=payload, headers=headers) as response:
            if response.status_code != 200:
                body = response.read().decode("utf-8", "replace")[:300]
                raise RuntimeError(f"LLM 接口返回 {response.status_code}: {body}")
            yield from _parse_sse_events(response.iter_lines())


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
    if name != "update_my_positions":
        return {"error": f"未知写工具: {name}"}
    rows = args.get("rows") if isinstance(args.get("rows"), list) else []
    if not rows:
        return {"error": "没有可保存的持仓明细 rows"}
    account = args.get("account") if isinstance(args.get("account"), dict) else {}
    default_account: dict[str, Any] = {**account}
    if default_account_id and "account_id" not in default_account:
        default_account["account_id"] = default_account_id
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


def _position_write_preview(name: str, result: dict) -> str:
    if "error" in result:
        return str(result["error"])
    return (
        f"已更新持仓：成功 {result.get('saved_count', 0)} 条，失败 {result.get('error_count', 0)} 条"
        f"（batch #{result.get('batch_id', '-')}, {result.get('ms', '-')}ms）"
    )


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
) -> Generator[dict, None, None]:
    stock_mode = chat_mode == "stock"
    digital_tool_names = digital_human_tool_names(digital_human_code) if not stock_mode else set()
    write_tool_names: set[str] = set()
    if stock_mode:
        tools = STOCK_TOOL_SCHEMAS
    else:
        tools = [*TOOLS, *digital_human_tool_schemas(digital_human_code)]
        if can_write_positions:
            w_schemas = write_tool_schemas()
            if w_schemas:
                tools = [*tools, *w_schemas]
                write_tool_names = {s["function"]["name"] for s in w_schemas}
    system_prompt = (
        stock_system_prompt_with_skills(digital_human_context, skills, memory_context)
        if stock_mode
        else _system_prompt(digital_human_context, digital_human_code, memory_context)
    )
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    for item in (history or [])[-8:]:
        if item.get("role") in {"user", "assistant"} and item.get("content"):
            messages.append({"role": item["role"], "content": item["content"]})
    messages.append({"role": "user", "content": question})

    llm_config = _active_llm_config(model_config_id)
    yield {
        "type": "model_selected",
        "model": {
            "id": llm_config.get("id"),
            "display_name": llm_config.get("display_name"),
            "model_name": llm_config.get("model"),
            "provider_name": llm_config.get("provider_name"),
        },
    }

    run_started = time.monotonic()
    completed_tool_label: str | None = None
    if stock_mode:
        yield {"type": "stage_start", "stage": "agent_loop", "message": "Starting agent analysis..."}
    for _ in range(llm_config["max_steps"]):
        message: dict | None = None
        if stock_mode:
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
            if stock_mode:
                yield {"type": "generating", "message": "正在生成最终分析..."}
            yield {"type": "content_end", "role": "answer"}
            if stock_mode:
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
            name = call.get("function", {}).get("name", "")
            try:
                args = json.loads(call.get("function", {}).get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}

            if stock_mode and name in ALLOWED_STOCK_TOOLS:
                display_name = stock_tool_display_name(name)
                yield {"type": "tool_start", "tool": name, "display_name": display_name}
                yield {"type": "action", "tool": name, "content": json.dumps(args, ensure_ascii=False)}
                tool_started = time.monotonic()
                result = execute_stock_tool(name, args)
                duration = round(time.monotonic() - tool_started, 2)
                yield {"type": "observation", "content": stock_observation_preview(name, result)}
                yield {
                    "type": "tool_done",
                    "tool": name,
                    "display_name": display_name,
                    "success": "error" not in result,
                    "duration": duration,
                }
                completed_tool_label = display_name
            elif not stock_mode and name in digital_tool_names:
                display_name = digital_human_tool_display_name(name)
                yield {"type": "action", "tool": name, "display_name": display_name, "content": json.dumps(args, ensure_ascii=False)}
                result = execute_digital_human_tool(digital_human_code, name, args)
                yield {"type": "observation", "content": digital_human_observation_preview(name, result)}
            elif write_tool_names and name in write_tool_names:
                display_name = "更新持仓" if name == "update_my_positions" else name
                yield {"type": "action", "tool": name, "display_name": display_name, "content": json.dumps(args, ensure_ascii=False)}
                result = _execute_position_write_tool(
                    name, args,
                    user_id=user_id or 0,
                    user_label=user_label or "unknown",
                    default_account_id=default_account_id,
                    digital_human_code=digital_human_code,
                )
                yield {"type": "observation", "content": _position_write_preview(name, result)}
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
            else:
                result = {"error": f"未知工具 {name}"}
                yield {"type": "observation", "content": result["error"]}

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": json.dumps(result, ensure_ascii=False)[:OBSERVATION_CHAR_LIMIT],
                }
            )

    yield {"type": "error", "content": f"已达到最大 ReAct 步数（{llm_config['max_steps']}），请把问题拆小后重试。"}
