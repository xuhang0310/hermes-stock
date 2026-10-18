from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select

from backend.app.agent_runtime.artifact_service import artifact_service
from backend.app.agent_runtime.contracts import EngineOutcome
from backend.app.agent_runtime.external_tools import external_tool_executor
from backend.app.agent_runtime.orchestration_tools import (
    ORCHESTRATION_PROMPT,
    ORCHESTRATION_TOOL_NAMES,
    SCHEDULE_MUTATION_TOOL_NAMES,
    TASK_HANDOFF_PROMPT,
    TASK_HANDOFF_TOOL_SCHEMAS,
    TEAM_AND_SCHEDULE_TOOL_SCHEMAS,
    TOOL_DISPLAY_NAMES as ORCHESTRATION_DISPLAY_NAMES,
)
from backend.app.agent_runtime.payloads import (
    materialize_agentscope_content,
    materialize_openai_content,
)
from backend.app.agent_runtime.tool_specs import should_externalize, tool_spec
from backend.app.agent_runtime.transaction_state import transaction_state_service
from backend.app.agents.tool_approval import (
    classify_tool_risk,
    create_tool_approval,
    requires_approval,
)
from backend.app.core.database import SessionLocal
from backend.app.core.llm_config import active_llm_config, reasoning_params
from backend.app.core.models import AgentToolApproval
from backend.app.core.settings import settings
from backend.app.market.digital_human_tools import digital_human_tool_display_name, write_tool_schemas
from backend.app.market.stock_tools import (
    stock_observation_preview,
    stock_system_prompt_with_skills,
    stock_tool_display_name,
)
from backend.app.skills.registry import skill_registry
from backend.app.skills.manifests import LOAD_SKILL_TOOL_SCHEMA


Emit = Callable[[dict[str, Any]], Awaitable[None]]
Checkpoint = Callable[[str, list[dict[str, Any]] | None], Awaitable[None]]
Cancelled = Callable[[], Awaitable[bool]]

AGENT_LOOP_HARD_MAX_ITERATIONS = 50


def _agent_loop_max_iterations() -> int:
    """Return the bounded AgentScope loop budget."""

    configured = int(getattr(settings, "agent_loop_max_iterations", 50) or 50)
    return max(1, min(configured, AGENT_LOOP_HARD_MAX_ITERATIONS))


def _agent_loop_duplicate_tool_limit() -> int:
    """Return how many identical read calls may run before the loop is stopped."""

    configured = int(getattr(settings, "agent_loop_duplicate_tool_limit", 3) or 3)
    return max(1, min(configured, 10))


class ReasoningLoopGuard:
    """Detect local repetition without treating recurring facts as a loop.

    Financial research legitimately repeats the same holding value in several
    distant sections (portfolio summary, risk review, and action plan).  A
    process-wide counter therefore produces false positives on long reports.
    Real model loops repeat within a much smaller recent window, so only keep
    counts for the latest eligible sentences.
    """

    def __init__(
        self,
        *,
        repeat_limit: int = 3,
        max_chars: int = 20_000,
        window_sentences: int = 120,
    ) -> None:
        self.repeat_limit = max(2, repeat_limit)
        self.max_chars = max(1_000, max_chars)
        self.window_sentences = max(self.repeat_limit, window_sentences)
        self.total_chars = 0
        self._tail = ""
        self._sentence_counts: dict[str, int] = {}
        self._recent_sentences: deque[str] = deque()

    def observe(self, delta: str) -> str | None:
        content = str(delta or "")
        if not content:
            return None
        self.total_chars += len(content)
        if self.total_chars > self.max_chars:
            return "reasoning_too_long"

        combined = self._tail + content
        sentences = re.split(r"[。！？!?；;\n]+", combined)
        self._tail = sentences.pop()[-500:]
        for sentence in sentences:
            normalized = re.sub(r"[\W_]+", "", sentence, flags=re.UNICODE).lower()
            if len(normalized) < 16:
                continue
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            if len(self._recent_sentences) >= self.window_sentences:
                expired = self._recent_sentences.popleft()
                remaining = self._sentence_counts.get(expired, 1) - 1
                if remaining > 0:
                    self._sentence_counts[expired] = remaining
                else:
                    self._sentence_counts.pop(expired, None)
            self._recent_sentences.append(digest)
            count = self._sentence_counts.get(digest, 0) + 1
            self._sentence_counts[digest] = count
            if count >= self.repeat_limit:
                return "repeated_reasoning"
        return None


def _reasoning_loop_error(
    request_payload: dict[str, Any],
    *,
    completed_business_write: bool,
    transaction_agent: bool,
) -> str:
    prefix = "检测到模型推理内容持续重复，系统已自动停止本轮任务，避免继续占用会话或重复写入。"
    if completed_business_write:
        return prefix + "本轮已有写入工具执行完成，请以工具返回结果为准。"
    if transaction_agent:
        return prefix + "本轮没有执行新的业务写入，请重新上传截图或发送更明确的单条指令。"
    if str(request_payload.get("source_type") or "") == "scheduler":
        return prefix + "本轮未生成有效报告，请缩小任务范围或检查所需数据是否完整后重试。"
    return prefix + "本轮没有执行新的业务写入，请缩小任务范围或发送更明确的单条指令。"


def _position_confirmation_requested(request_payload: dict[str, Any]) -> bool:
    if str(request_payload.get("digital_human_code") or "") != "position-entry-agent":
        return False
    question = str(request_payload.get("user_text") or request_payload.get("question") or "").strip()
    normalized = re.sub(r"[\s，。！？!?、；;：:]+", "", question)
    return normalized in {
        "确认",
        "确定",
        "好的",
        "可以",
        "保存",
        "入库",
        "确认保存",
        "确认入库",
        "保存进去",
        "直接保存",
        "保存就行",
        "保存一下",
        "入库就行",
    }


def _transaction_preflight_reply(request_payload: dict[str, Any]) -> str | None:
    """Reject history-only save confirmations before invoking the model."""

    if not _position_confirmation_requested(request_payload):
        return None

    question = str(request_payload.get("user_text") or request_payload.get("question") or "").strip()
    attachments = request_payload.get("attachments")
    has_current_image = bool(
        request_payload.get("image_sha256")
        or (
            isinstance(attachments, list)
            and any(
                isinstance(item, dict)
                and (
                    item.get("kind") == "image"
                    or str(item.get("mime_type") or "").startswith("image/")
                )
                for item in attachments
            )
        )
    )
    pending = request_payload.get("pending_action")
    if not pending and request_payload.get("session_id"):
        state = transaction_state_service.load(
            int(request_payload["session_id"]),
            "position-entry-agent",
        )
        pending = (state or {}).get("pending_action")
    has_current_facts = bool(
        re.search(r"(?<!\d)\d{6}(?:\.(?:SH|SZ|BJ))?(?!\d)", question.upper())
        or re.search(r"\d[\d,]*(?:\.\d+)?\s*(?:股|手)", question)
    )
    if has_current_image or pending or has_current_facts:
        return None
    return (
        "当前会话没有可确认的 OCR 持仓草稿，本轮也没有收到新的截图或持仓明细。"
        "为避免把历史聊天中的旧持仓重复写入，我已停止本次保存；请重新上传截图，"
        "或直接输入“股票代码、数量、成本价”，系统会重新识别并让你核对后入库。"
    )


def agentscope_available() -> tuple[bool, str | None]:
    try:
        import agentscope

        return True, str(agentscope.__version__)
    except Exception:  # noqa: BLE001
        return False, None


def select_engine(chat_mode: str, configured_mode: str) -> str:
    mode = configured_mode.strip().lower()
    if mode == "agentscope":
        return "agentscope"
    if mode == "agentscope-stock" and chat_mode == "stock":
        return "agentscope"
    return "legacy"


def agent_llm_wire_api(llm_config: dict[str, Any] | None = None) -> str:
    """Resolve the AgentScope OpenAI wire protocol.

    The model config may provide ``wire_api`` in the future; the deployment
    setting remains the current public switch. Unknown values intentionally
    fall back to Responses so a typo cannot silently regress to the old API.
    """

    configured = str(
        (llm_config or {}).get("wire_api")
        or getattr(settings, "agent_llm_wire_api", "responses")
        or "responses"
    ).strip().lower()
    if configured in {"chat", "chat_completion", "chat_completions", "completions"}:
        return "chat_completions"
    return "responses"


def normalize_agent_base_url(base_url: str) -> str:
    """Normalize a configured OpenAI-compatible base URL to its API root."""

    normalized = str(base_url or "").rstrip("/")
    for suffix in ("/chat/completions", "/chat/completion", "/responses"):
        if normalized.lower().endswith(suffix):
            return normalized[: -len(suffix)].rstrip("/")
    return normalized


def responses_reasoning_effort(
    llm_config: dict[str, Any], reasoning: dict[str, Any]
) -> str | None:
    """Map the existing provider-neutral reasoning settings to Responses."""

    if reasoning.get("reasoning_effort"):
        return str(reasoning["reasoning_effort"])
    if not reasoning.get("enable_thinking"):
        return None
    level = str(llm_config.get("reasoning_effort") or "medium").lower()
    return {"low": "low", "medium": "medium", "high": "high"}.get(level, "medium")


def _builtin_backtest_route(question: str) -> str | None:
    normalized = str(question or "").strip()
    if not normalized:
        return None
    has_backtest_intent = any(
        term in normalized
        for term in ("回测", "历史验证", "策略验证", "收益测试", "净值曲线")
    )
    if not has_backtest_intent:
        return None
    # 动量/横截面意图交给 LLM 按 prompt_instructions 选 run_signal_backtest(momentum)，
    # 否则"多只股票"会被下面的规则误导向风险平价组合
    if any(term in normalized for term in ("动量", "强者恒强", "横截面")):
        return None
    symbols = re.findall(r"(?<!\d)\d{6}(?:\.(?:SH|SZ|BJ))?(?!\d)", normalized.upper())
    if (
        any(term in normalized for term in ("风险平价", "等权", "组合回测", "组合归因"))
        or len(set(symbols)) >= 2
    ):
        return "run_portfolio_backtest"
    if "MACD" in normalized.upper():
        return "run_macd_backtest"
    return None


def _apply_builtin_backtest_route(
    system_prompt: str,
    tools: list[dict],
    question: str,
) -> tuple[str, list[dict]]:
    route = _builtin_backtest_route(question)
    if not route:
        return system_prompt, tools
    blocked = {
        "ws_run_python",
        "ws_write_file",
        "ws_edit_file",
        "run_macd_backtest" if route == "run_portfolio_backtest" else "run_portfolio_backtest",
    }
    routed_tools = [
        item
        for item in tools
        if item.get("function", {}).get("name") not in blocked
    ]
    route_label = "风险平价组合" if route == "run_portfolio_backtest" else "MACD"
    system_prompt += (
        f"\n\n当前请求已命中系统内置{route_label}回测能力。"
        f"必须直接调用 `{route}`，禁止编写、修改或执行临时 Python 回测脚本。"
        "工具返回后应优先展示结构化摘要与完整报告链接。"
    )
    return system_prompt, routed_tools


def _tool_display_name(name: str) -> str:
    if name in ORCHESTRATION_DISPLAY_NAMES:
        return ORCHESTRATION_DISPLAY_NAMES[name]
    label = digital_human_tool_display_name(name)
    return label if label != name else stock_tool_display_name(name)


def _tool_preview(name: str, result: dict[str, Any]) -> str:
    if "error" in result:
        return str(result["error"])
    if name == "list_workflow_templates":
        return f"可用 Swarm 工作流模板 {len(result.get('templates', []))} 套。"
    if name == "run_agent_workflow":
        return str(result.get("summary") or f"Swarm 工作流 {result.get('status', '已完成')}。")
    if name == "list_delegatable_agents":
        return f"可委派数字人 {len(result.get('agents', []))} 个。"
    if name == "dispatch_agent_tasks":
        return str(result.get("summary") or f"Agent Team {result.get('status', '已完成')}。")
    if name == "create_agent_task_batch":
        return str(result.get("summary") or f"任务批次 {result.get('status', '已创建')}，{result.get('created_count', 0)} 个子任务。")
    if name == "list_agent_schedules":
        return (
            f"定时任务共 {int(result.get('total', len(result.get('jobs', []))))} 个，"
            f"其中启用 {int(result.get('enabled_total', 0))} 个。"
        )
    if name == "get_agent_schedule":
        job = result.get("job") if isinstance(result.get("job"), dict) else {}
        return f"已读取定时任务：{job.get('name') or job.get('job_key') or '-'}。"
    if name in SCHEDULE_MUTATION_TOOL_NAMES:
        job = result.get("job") if isinstance(result.get("job"), dict) else {}
        return f"定时任务操作完成：{job.get('name') or result.get('job_key') or '-'}。"
    from backend.app.market.digital_human_tools import digital_human_observation_preview

    preview = stock_observation_preview(name, result)
    return preview if preview != "工具调用完成。" else digital_human_observation_preview(name, result)


def _verified_schedule_receipt(
    tool_name: str,
    result: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Accept only schedule mutations whose database postcondition was verified."""

    if tool_name not in SCHEDULE_MUTATION_TOOL_NAMES:
        return None, "不是定时任务写操作。"
    if result.get("error"):
        return None, str(result["error"])
    if result.get("verified") is not True:
        return None, "工具没有返回数据库核验标记 verified=true。"

    job = result.get("job") if isinstance(result.get("job"), dict) else None
    if tool_name == "create_agent_schedule":
        if result.get("created") is not True and result.get("already_exists") is not True:
            return None, "创建工具没有返回 created=true 或 already_exists=true。"
        if not job or not str(job.get("job_key") or "").strip():
            return None, "创建工具没有返回数据库中的任务编号。"
    elif tool_name in {"update_agent_schedule", "pause_agent_schedule"}:
        if result.get("updated") is not True or not job or not str(job.get("job_key") or "").strip():
            return None, "修改工具没有返回已核验的任务记录。"
    elif tool_name == "delete_agent_schedule":
        if result.get("deleted") is not True or not str(result.get("job_key") or "").strip():
            return None, "删除工具没有返回已核验的任务编号。"

    return {
        "tool": tool_name,
        "created": result.get("created") is True,
        "already_exists": result.get("already_exists") is True,
        "job": dict(job or {}),
        "job_key": str((job or {}).get("job_key") or result.get("job_key") or ""),
    }, None


def _schedule_description(job: dict[str, Any]) -> str:
    schedule_type = str(job.get("schedule_type") or "")
    if schedule_type == "once":
        return f"一次性执行：{job.get('run_once_at') or '-'}"
    if schedule_type == "daily":
        return f"每日 {job.get('run_time') or '-'}"
    if schedule_type == "weekly":
        return f"每周 {job.get('weekday_mask') or '-'} 的 {job.get('run_time') or '-'}"
    if schedule_type == "monthly_last":
        return f"每月最后一天 {job.get('run_time') or '-'}"
    if schedule_type == "interval":
        return (
            f"每 {job.get('interval_minutes') or '-'} 分钟，"
            f"窗口 {job.get('window_start') or '-'}–{job.get('window_end') or '-'}"
        )
    return schedule_type or "-"


def _schedule_receipt_answer(receipts: list[dict[str, Any]]) -> str:
    """Build the final schedule answer only from verified tool results."""

    sections: list[str] = []
    labels = {
        "update_agent_schedule": "定时任务已修改并通过数据库核验。",
        "pause_agent_schedule": "定时任务启停状态已修改并通过数据库核验。",
        "delete_agent_schedule": "定时任务已删除并通过数据库核验。",
    }
    for receipt in receipts:
        tool_name = str(receipt.get("tool") or "")
        job = receipt.get("job") if isinstance(receipt.get("job"), dict) else {}
        if tool_name == "create_agent_schedule":
            heading = (
                "同名定时任务已存在且配置一致，数据库核验通过；未重复创建。"
                if receipt.get("already_exists")
                else "定时任务已创建并通过数据库核验。"
            )
        else:
            heading = labels.get(tool_name, "定时任务操作已通过数据库核验。")
        lines = [heading, f"- 任务编号：{receipt.get('job_key') or '-'}"]
        if job:
            lines.extend(
                [
                    f"- 任务名称：{job.get('name') or '-'}",
                    f"- 执行者：{job.get('target_agent_code') or '-'}",
                    f"- 执行时间：{_schedule_description(job)}",
                    f"- 交易日限定：{'是' if job.get('trading_day_only') else '否'}",
                    f"- 飞书推送：{'是' if job.get('push_to_feishu') else '否'}",
                    f"- 状态：{'已启用' if job.get('enabled') else '已停用'}",
                ]
            )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _build_definition(request_payload: dict[str, Any]) -> tuple[str, list[dict], set[str]]:
    from backend.app.agents.stock_agent import TOOLS, _knowledge_system_prompt, _system_prompt

    chat_mode = str(request_payload.get("chat_mode") or "research")
    role_code = request_payload.get("digital_human_code")
    memory_context = str(request_payload.get("memory_context") or "")
    skills = list(request_payload.get("skills") or [])
    explicitly_selected_names = list(request_payload.get("selected_skill_names") or [])
    question = str(request_payload.get("user_text") or request_payload.get("question") or "")
    selected_manifests = skill_registry.resolve_manifests(
        question,
        chat_mode,
        role_code,
        selected_names=explicitly_selected_names,
    )
    request_payload["resolved_skills"] = [item.name for item in selected_manifests]
    progressive_context = skill_registry.build_progressive_context(
        selected_manifests, chat_mode, role_code
    )
    explicitly_selected = [
        item for item in selected_manifests if item.name in explicitly_selected_names
    ]
    explicit_progressive_context = (
        skill_registry.build_progressive_context(explicitly_selected, chat_mode, role_code)
        if explicitly_selected
        else ""
    )
    write_names: set[str] = set()
    if role_code == "position-entry-agent":
        from backend.app.agents.position_agent import POSITION_AGENT_TOOLS, _position_agent_prompt

        write_names = {"save_position_holdings"}
        return (
            _position_agent_prompt()
            + transaction_state_service.prompt_context(request_payload)
            + (f"\n\n{explicit_progressive_context}" if explicit_progressive_context else ""),
            [*POSITION_AGENT_TOOLS, *([LOAD_SKILL_TOOL_SCHEMA] if explicitly_selected else [])],
            write_names,
        )
    if role_code == "watchlist-entry-agent":
        from backend.app.agents.watchlist_agent import WATCHLIST_AGENT_TOOLS, _watchlist_agent_prompt

        write_names = {"add_watchlist_items", "remove_watchlist_items"}
        return (
            _watchlist_agent_prompt()
            + transaction_state_service.prompt_context(request_payload)
            + (f"\n\n{explicit_progressive_context}" if explicit_progressive_context else ""),
            [*WATCHLIST_AGENT_TOOLS, *([LOAD_SKILL_TOOL_SCHEMA] if explicitly_selected else [])],
            write_names,
        )
    if chat_mode == "stock":
        system_prompt, tools = _apply_builtin_backtest_route(
            stock_system_prompt_with_skills(
                request_payload.get("digital_human_context"), skills, memory_context
            )
            + (f"\n\n{progressive_context}" if progressive_context else ""),
            [*skill_registry.get_tools_for("stock"), LOAD_SKILL_TOOL_SCHEMA],
            question,
        )
        return (
            system_prompt,
            tools,
            write_names,
        )
    if chat_mode == "knowledge":
        system_prompt, tools = _apply_builtin_backtest_route(
            _knowledge_system_prompt(memory_context)
            + (f"\n\n{progressive_context}" if progressive_context else ""),
            [*TOOLS, LOAD_SKILL_TOOL_SCHEMA],
            question,
        )
        return (
            system_prompt,
            tools,
            write_names,
        )

    tools = [
        *TOOLS,
        *skill_registry.get_tools_for("research", role_code=role_code),
        LOAD_SKILL_TOOL_SCHEMA,
    ]
    delegation_config = request_payload.get("delegation_config") or {}
    structured_task_handoff = (
        isinstance(delegation_config, dict)
        and delegation_config.get("mode") != "none"
        and delegation_config.get("extraction_mode") == "structured_tool"
    )
    if role_code == "stock-pm":
        tools.extend(TEAM_AND_SCHEDULE_TOOL_SCHEMAS)
        write_names.update(SCHEDULE_MUTATION_TOOL_NAMES)
    elif structured_task_handoff:
        tools.extend(TASK_HANDOFF_TOOL_SCHEMAS)
    # delegation_mode=none 时移除所有编排工具
    if isinstance(delegation_config, dict) and delegation_config.get("mode") == "none":
        blocked_orchestration = {"dispatch_agent_tasks", "create_agent_task_batch", "run_agent_workflow"}
        tools = [item for item in tools if item.get("function", {}).get("name") not in blocked_orchestration]
    if request_payload.get("can_write_positions"):
        schemas = write_tool_schemas()
        tools.extend(schemas)
        write_names.update(item["function"]["name"] for item in schemas)
    if not request_payload.get("allow_code_execution"):
        tools = [item for item in tools if item.get("function", {}).get("name") != "ws_run_python"]
    if request_payload.get("delegation_read_only"):
        blocked = {
            "ws_write_file",
            "ws_edit_file",
            "ws_run_python",
            "save_position_holdings",
            "add_watchlist_items",
            "remove_watchlist_items",
            "update_my_positions",
            "update_position_field",
            "rollback_last_position_batch",
            *ORCHESTRATION_TOOL_NAMES,
        }
        tools = [item for item in tools if item.get("function", {}).get("name") not in blocked]
        write_names.clear()
    system_prompt = _system_prompt(request_payload.get("digital_human_context"), role_code, memory_context)
    if progressive_context:
        system_prompt += f"\n\n{progressive_context}"
    if role_code == "stock-pm":
        system_prompt += ORCHESTRATION_PROMPT
    elif structured_task_handoff:
        system_prompt += TASK_HANDOFF_PROMPT
    active_artifacts = artifact_service.active_context(
        user_id=int(request_payload["user_id"]),
        session_id=int(request_payload["session_id"]) if request_payload.get("session_id") else None,
    )
    if active_artifacts:
        system_prompt += f"\n\n{active_artifacts}"
    system_prompt, tools = _apply_builtin_backtest_route(system_prompt, tools, question)
    return system_prompt, tools, write_names


def _execute_tool(
    name: str,
    args: dict[str, Any],
    request_payload: dict[str, Any],
    write_names: set[str],
) -> dict[str, Any]:
    from backend.app.agents.stock_agent import (
        _execute_position_write_tool,
        get_schema,
        run_sql,
        search_knowledge,
    )

    args, transaction_state = transaction_state_service.before_tool(request_payload, name, args)
    write_signature: str | None = None
    completed_write_signatures = request_payload.setdefault("_completed_write_signatures", set())
    if name in write_names:
        canonical_args = json.dumps(
            args,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        write_signature = f"{name}:{hashlib.sha256(canonical_args.encode('utf-8')).hexdigest()}"
        if write_signature in completed_write_signatures:
            return {
                "error": "检测到同一写入请求被重复调用，系统已自动拦截，未再次写入。",
                "duplicate_write_blocked": True,
                "saved_count": 0,
                "changed_count": 0,
                "rows": [],
            }
    role_code = str(request_payload.get("digital_human_code") or "")
    if name == "load_skill":
        return skill_registry.load_manifest(
            str(args.get("skill_name") or ""),
            str(args.get("resource_path") or "") or None,
        )
    if role_code == "position-entry-agent":
        from backend.app.agents.position_agent import _execute_position_tool
        from backend.app.data.repository import StockRepository

        account_id = (transaction_state or {}).get("slots", {}).get("account_id")
        account_id = request_payload.get("default_account_id") or account_id
        with SessionLocal() as db:
            result = _execute_position_tool(
                repository=StockRepository(db, owner_user_id=int(request_payload["user_id"])),
                name=name,
                args=args,
                created_by=str(request_payload.get("user_label") or "agent"),
                source_name="AgentScope",
                image_mime_type=str(request_payload.get("image_mime_type") or "image/png"),
                image_sha256=request_payload.get("image_sha256"),
                account_id=int(account_id) if account_id else None,
                instruction=str(request_payload.get("user_text") or request_payload.get("question") or ""),
                trusted_pending_action=bool((transaction_state or {}).get("pending_action")),
            )
        transaction_state_service.after_tool(request_payload, name, args, result)
        if (
            write_signature
            and "error" not in result
            and int(result.get("saved_count") or result.get("changed_count") or 0) > 0
        ):
            completed_write_signatures.add(write_signature)
        return result
    if role_code == "watchlist-entry-agent":
        from backend.app.agents.watchlist_agent import _execute_watchlist_tool
        from backend.app.data.repository import StockRepository

        with SessionLocal() as db:
            result = _execute_watchlist_tool(
                StockRepository(db, owner_user_id=int(request_payload["user_id"])),
                name,
                args,
                created_by=str(request_payload.get("user_label") or "agent"),
            )
        transaction_state_service.after_tool(request_payload, name, args, result)
        if (
            write_signature
            and "error" not in result
            and int(result.get("saved_count") or result.get("changed_count") or 0) > 0
        ):
            completed_write_signatures.add(write_signature)
        return result

    if name == "run_sql":
        return run_sql(str(args.get("sql") or ""))
    if name == "get_schema":
        return get_schema(str(args.get("table_name") or ""))
    if name == "search_knowledge":
        return search_knowledge(
            str(args.get("query") or ""),
            top_k=args.get("top_k"),
            symbol=str(args.get("symbol") or "") or None,
            doc_types=args.get("doc_types") if isinstance(args.get("doc_types"), list) else None,
            trust_levels=args.get("trust_levels") if isinstance(args.get("trust_levels"), list) else None,
        )
    if name == "list_delegatable_agents":
        from backend.app.agent_runtime.team_service import agent_team_service

        return {
            "agents": agent_team_service.list_delegatable_agents(
                int(request_payload["user_id"]),
            )
        }
    if name == "list_workflow_templates":
        from backend.app.agent_runtime.workflow_service import agent_workflow_service

        return {"templates": agent_workflow_service.list_templates()}
    if name in SCHEDULE_MUTATION_TOOL_NAMES or name in {
        "list_agent_schedules",
        "get_agent_schedule",
    }:
        from backend.app.scheduler.schedule_service import agent_schedule_service

        try:
            return agent_schedule_service.execute(name, args, request_payload)
        except (PermissionError, ValueError) as exc:
            # These failures happen before a schedule mutation is committed.
            # Return the concrete business error so the durable worker does not
            # misclassify a deterministic rejection as an unknown side effect.
            return {
                "error": str(exc),
                "error_code": "schedule_validation_error",
                "verified": False,
            }
    if name in write_names:
        return _execute_position_write_tool(
            name,
            args,
            user_id=int(request_payload["user_id"]),
            user_label=str(request_payload.get("user_label") or "unknown"),
            default_account_id=request_payload.get("default_account_id"),
            digital_human_code=request_payload.get("digital_human_code"),
        )
    if name in {"run_macd_backtest", "run_portfolio_backtest", "run_signal_backtest", "run_factor_backtest"}:
        from backend.app.skills.strategy_backtest import (
            run_backtest_tool,
            run_factor_backtest_tool,
            run_portfolio_backtest_tool,
            run_signal_backtest_tool,
        )

        owner_user_id = int(request_payload["user_id"])
        agent_run_id = str(request_payload.get("run_id") or "") or None
        if name == "run_macd_backtest":
            return run_backtest_tool(args, owner_user_id=owner_user_id, agent_run_id=agent_run_id)
        if name == "run_portfolio_backtest":
            return run_portfolio_backtest_tool(args, owner_user_id=owner_user_id, agent_run_id=agent_run_id)
        executor = run_signal_backtest_tool if name == "run_signal_backtest" else run_factor_backtest_tool
        return executor(args, owner_user_id=owner_user_id)
    if skill_registry.has_tool(name):
        from backend.app.data.user_scope import data_user_scope

        if name.startswith("ws_"):
            from backend.app.skills.agent_workspace import workspace_execution_scope, workspace_user_scope

            with (
                data_user_scope(int(request_payload["user_id"])),
                workspace_user_scope(int(request_payload["user_id"])),
                workspace_execution_scope(bool(request_payload.get("allow_code_execution"))),
            ):
                return skill_registry.execute(name, args)
        with data_user_scope(int(request_payload["user_id"])):
            return skill_registry.execute(name, args)
    return {"error": f"未知工具 {name}"}


def _build_tools(request_payload: dict[str, Any], schemas: list[dict], write_names: set[str]):
    from agentscope.message import TextBlock, ToolResultState
    from agentscope.permission import PermissionBehavior, PermissionDecision
    from agentscope.tool import ToolBase, ToolChunk

    approval_policy = str(request_payload.get("approval_policy") or "ask")
    duplicate_tool_limit = _agent_loop_duplicate_tool_limit()
    read_call_counts: dict[str, int] = {}

    class HermesTool(ToolBase):
        def __init__(self, schema: dict[str, Any]) -> None:
            super().__init__()
            function = schema["function"]
            self.name = str(function["name"])
            self.description = str(function.get("description") or self.name)
            self.input_schema = dict(function.get("parameters") or {"type": "object", "properties": {}})
            self.spec = tool_spec(self.name, write_names=write_names)
            self.is_concurrency_safe = self.spec.is_concurrency_safe
            self.is_read_only = self.spec.is_read_only
            self.is_external_tool = self.name in {"dispatch_agent_tasks", "run_agent_workflow", "create_agent_task_batch"}

        async def check_permissions(self, tool_input, _context):
            risk = classify_tool_risk(self.name, tool_input, write_names)
            if requires_approval(approval_policy, risk):
                return PermissionDecision(
                    behavior=PermissionBehavior.ASK,
                    message=f"{_tool_display_name(self.name)} 需要用户审批",
                )
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message=f"{_tool_display_name(self.name)} 已按策略授权",
            )

        async def call(self, **kwargs):
            try:
                execution_id = None
                if self.is_read_only:
                    canonical_input = json.dumps(
                        kwargs,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    )
                    signature = hashlib.sha256(
                        f"{self.name}:{canonical_input}".encode("utf-8")
                    ).hexdigest()
                    call_count = read_call_counts.get(signature, 0) + 1
                    read_call_counts[signature] = call_count
                    if call_count > duplicate_tool_limit:
                        result = {
                            "error": (
                                f"相同参数的只读工具 {self.name} 在本轮已执行 "
                                f"{duplicate_tool_limit} 次，AgentLoop 已停止重复调用。"
                            ),
                            "duplicate_tool_call_blocked": True,
                            "tool": self.name,
                            "duplicate_count": call_count,
                            "allowed_count": duplicate_tool_limit,
                        }
                        return ToolChunk(
                            content=[
                                TextBlock(
                                    text=json.dumps(result, ensure_ascii=False, default=str)
                                )
                            ],
                            state=ToolResultState.ERROR,
                            metadata={
                                "result": result,
                                "display_name": _tool_display_name(self.name),
                                "execution_id": None,
                            },
                        )
                externalized = should_externalize(
                    self.spec,
                    enabled=bool(settings.tool_external_execution_enabled and request_payload.get("run_id")),
                )
                if externalized:
                    result, execution_id = await external_tool_executor.execute(
                        run_id=str(request_payload["run_id"]),
                        tool_name=self.name,
                        arguments=kwargs,
                        spec=self.spec,
                    )
                else:
                    result = await asyncio.to_thread(
                        _execute_tool, self.name, kwargs, request_payload, write_names
                    )
                    if self.name in {"ws_write_file", "ws_edit_file"} and result.get("path"):
                        result = {**result, "artifacts": [{"path": result["path"]}]}
                    result = await asyncio.to_thread(
                        artifact_service.register_result,
                        result=result,
                        user_id=int(request_payload["user_id"]),
                        session_id=(
                            int(request_payload["session_id"])
                            if request_payload.get("session_id")
                            else None
                        ),
                        run_id=str(request_payload.get("run_id") or "") or None,
                        created_by=str(request_payload.get("user_label") or "agent"),
                    )
                state = ToolResultState.ERROR if "error" in result else ToolResultState.SUCCESS
            except Exception as exc:  # noqa: BLE001
                result = {"error": str(exc)}
                execution_id = None
                state = ToolResultState.ERROR
            return ToolChunk(
                content=[TextBlock(text=json.dumps(result, ensure_ascii=False, default=str))],
                state=state,
                metadata={
                    "result": result,
                    "display_name": _tool_display_name(self.name),
                    "execution_id": execution_id,
                },
            )

    return [HermesTool(schema) for schema in schemas]


def _position_value(value: Any, *, digits: int = 4) -> str:
    if value is None or value == "":
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.{digits}f}".rstrip("0").rstrip(".")


def _position_account_label(arguments: dict[str, Any]) -> str:
    account = arguments.get("account") if isinstance(arguments.get("account"), dict) else {}
    parts = [
        str(account.get("broker_name") or "").strip(),
        str(account.get("account_name") or "").strip(),
        str(account.get("account_no_masked") or "").strip(),
    ]
    return " / ".join(item for item in parts if item) or "当前选择的证券账户"


def _position_draft_reply(arguments: dict[str, Any], result: dict[str, Any]) -> str:
    rows = result.get("rows") if isinstance(result.get("rows"), list) else []
    validations = result.get("validations") if isinstance(result.get("validations"), list) else []
    validation_by_index = {
        int(item.get("index") or 0): item for item in validations if isinstance(item, dict)
    }
    lines = [
        f"已识别 **{len(rows)} 条**持仓，待保存到 **{_position_account_label(arguments)}**。",
        "",
        "| # | 名称 | 代码 | 数量 | 成本价 | 现价 | 校验 |",
        "|---:|---|---|---:|---:|---:|---|",
    ]
    invalid_count = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        validation = validation_by_index.get(index, {})
        valid = bool(validation.get("valid"))
        invalid_count += 0 if valid else 1
        lines.append(
            "| {index} | {name} | {symbol} | {quantity} | {cost} | {latest} | {status} |".format(
                index=index + 1,
                name=str(row.get("name") or "-"),
                symbol=str(row.get("symbol") or "-"),
                quantity=_position_value(row.get("position_quantity"), digits=0),
                cost=_position_value(row.get("cost_price")),
                latest=_position_value(row.get("latest_price")),
                status="通过" if valid else str(validation.get("message") or "需纠正"),
            )
        )
    lines.extend(["", f"持仓日期：{arguments.get('as_of_date') or '以截图日期为准'}。"])
    if invalid_count:
        lines.append(f"其中 **{invalid_count} 条代码需要纠正**，修正后再回复“确认”。")
    else:
        lines.append("股票代码已完成校验。请核对明细，回复 **“确认”** 后一次性入库。")
    return "\n".join(lines)


def _position_save_reply(result: dict[str, Any]) -> str:
    if result.get("need_account"):
        return str(result.get("message") or "请确认这批持仓要保存到哪个证券账户。")
    if result.get("need_review"):
        return _position_draft_reply({}, result)
    if result.get("error"):
        return f"持仓未保存：{result['error']}"
    saved = int(result.get("saved_count") or 0)
    rows = result.get("rows") if isinstance(result.get("rows"), list) else []
    names = [
        str(item.get("name") or item.get("symbol") or "")
        for item in rows
        if isinstance(item, dict) and not item.get("error")
    ]
    detail = "、".join(item for item in names if item)
    return (
        f"已确认入库，成功保存 **{saved} 条**持仓。"
        + (f"\n\n本次保存：{detail}。" if detail else "")
    )


async def _emit_position_tool_result(
    *,
    emit: Emit,
    tool_call_id: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> None:
    await emit(
        {
            "type": "action",
            "tool_call_id": tool_call_id,
            "tool": "save_position_holdings",
            "display_name": _tool_display_name("save_position_holdings"),
            "content": json.dumps(arguments, ensure_ascii=False, default=str),
            "risk_level": "medium",
            "has_side_effects": True,
        }
    )
    await emit({"type": "observation", "content": _tool_preview("save_position_holdings", result)})
    await emit(
        {
            "type": "tool_done",
            "tool_call_id": tool_call_id,
            "tool": "save_position_holdings",
            "display_name": _tool_display_name("save_position_holdings"),
            "success": "error" not in result,
        }
    )


async def _confirm_pending_position_draft(
    request_payload: dict[str, Any],
    emit: Emit,
) -> EngineOutcome | None:
    if not _position_confirmation_requested(request_payload) or not request_payload.get("session_id"):
        return None
    state = await asyncio.to_thread(
        transaction_state_service.load,
        int(request_payload["session_id"]),
        "position-entry-agent",
    )
    pending = (state or {}).get("pending_action")
    if not pending:
        return None
    arguments = pending.get("arguments") if isinstance(pending, dict) else {}
    if not isinstance(arguments, dict):
        return None
    result = await asyncio.to_thread(
        _execute_tool,
        "save_position_holdings",
        {},
        request_payload,
        {"save_position_holdings"},
    )
    await _emit_position_tool_result(
        emit=emit,
        tool_call_id="position_confirmation",
        arguments=arguments,
        result=result,
    )
    reply = _position_save_reply(result)
    await emit({"type": "content_delta", "content": reply})
    await emit({"type": "content_end", "role": "answer"})
    await emit(
        {
            "type": "stage_done",
            "stage": "position_confirmation",
            "status": "completed",
            "iterations": 0,
        }
    )
    return EngineOutcome(final_answer=reply)


async def _stage_position_ocr_draft(
    request_payload: dict[str, Any],
    emit: Emit,
) -> EngineOutcome | None:
    if (
        str(request_payload.get("digital_human_code") or "") != "position-entry-agent"
        or not request_payload.get("image_sha256")
    ):
        return None
    from backend.app.agents.position_agent import extract_position_ocr_arguments

    await emit(
        {
            "type": "stage_start",
            "stage": "position_ocr",
            "message": "正在识别截图并校验股票代码...",
        }
    )
    extracted = await asyncio.to_thread(
        extract_position_ocr_arguments,
        user_content=materialize_openai_content(request_payload),
        model_config_id=request_payload.get("model_config_id"),
    )
    await emit({"type": "model_selected", "model": extracted["model"]})
    arguments = extracted["arguments"]
    result = await asyncio.to_thread(
        _execute_tool,
        "save_position_holdings",
        arguments,
        request_payload,
        {"save_position_holdings"},
    )
    await _emit_position_tool_result(
        emit=emit,
        tool_call_id="position_ocr_draft",
        arguments=arguments,
        result=result,
    )
    reply = (
        _position_draft_reply(arguments, result)
        if result.get("need_review")
        else _position_save_reply(result)
    )
    await emit({"type": "content_delta", "content": reply})
    await emit({"type": "content_end", "role": "answer"})
    await emit(
        {
            "type": "stage_done",
            "stage": "position_ocr",
            "status": "completed",
            "iterations": 1,
        }
    )
    return EngineOutcome(final_answer=reply)


def _history_state(request_payload: dict[str, Any]):
    from agentscope.message import AssistantMsg, UserMsg
    from agentscope.state import AgentState

    context = []
    for item in request_payload.get("history") or []:
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        if item.get("role") == "assistant":
            context.append(AssistantMsg("assistant", content, finished_at=item.get("created_at")))
        elif item.get("role") == "user":
            context.append(UserMsg("user", content, created_at=item.get("created_at")))
    return AgentState(session_id=str(request_payload["session_id"]), context=context)


def _resume_event(run_id: str, state):
    from agentscope.event import (
        ConfirmResult,
        ExternalExecutionResultEvent,
        UserConfirmResultEvent,
    )
    from agentscope.message import TextBlock, ToolResultBlock, ToolResultState

    with SessionLocal() as db:
        approvals = db.scalars(
            select(AgentToolApproval).where(AgentToolApproval.run_id == run_id).order_by(AgentToolApproval.created_at)
        ).all()
    by_call = {row.tool_call_id: row for row in approvals if row.tool_call_id}
    pending_calls = []
    if state.context:
        pending_calls = list(state.context[-1].get_content_blocks("tool_call"))
    results = []
    resolved_events: list[dict[str, Any]] = []
    for tool_call in pending_calls:
        if str(tool_call.state) != "asking":
            continue
        row = by_call.get(tool_call.id)
        if row is None or row.status == "pending":
            raise RuntimeError("Agent approval state is incomplete; refusing an unsafe resume.")
        results.append(ConfirmResult(confirmed=row.status == "approved", tool_call=tool_call))
        resolved_events.append(
            {
                "type": "approval_resolved",
                "approval_id": row.approval_id,
                "tool_call_id": tool_call.id,
                "tool": row.tool_name,
                "display_name": row.display_name,
                "decision": row.status,
            }
        )
    if results:
        return UserConfirmResultEvent(reply_id=state.reply_id, confirm_results=results), resolved_events

    from backend.app.agent_runtime.team_service import agent_team_service
    from backend.app.agent_runtime.workflow_service import agent_workflow_service

    execution_results = []
    for tool_call in pending_calls:
        if str(tool_call.state) != "submitted":
            continue
        if tool_call.name == "run_agent_workflow":
            result = agent_workflow_service.result_for_tool_call(run_id, tool_call.id)
        else:
            result = agent_team_service.result_for_tool_call(run_id, tool_call.id)
        if result is None:
            raise RuntimeError("Agent Team 尚未完成，拒绝提前恢复总调度。")
        failed = result.get("status") == "failed"
        execution_results.append(
            ToolResultBlock(
                id=tool_call.id,
                name=tool_call.name,
                output=[TextBlock(text=json.dumps(result, ensure_ascii=False, default=str))],
                state=ToolResultState.ERROR if failed else ToolResultState.SUCCESS,
                metadata={"result": result, "display_name": _tool_display_name(tool_call.name)},
            )
        )
        resolved_events.append(
            {
                "type": (
                    "workflow_resumed"
                    if tool_call.name == "run_agent_workflow"
                    else "team_resumed"
                ),
                "team_id": result.get("team_id"),
                "workflow_run_id": result.get("workflow_run_id"),
                "tool_call_id": tool_call.id,
                "status": result.get("status"),
                "summary": result.get("summary"),
            }
        )
    if execution_results:
        return (
            ExternalExecutionResultEvent(
                reply_id=state.reply_id,
                execution_results=execution_results,
            ),
            resolved_events,
        )
    return None, resolved_events


async def run_agentscope(
    *,
    run_id: str,
    request_payload: dict[str, Any],
    checkpoint_json: str | None,
    emit: Emit,
    save_checkpoint: Checkpoint,
    is_cancelled: Cancelled,
) -> EngineOutcome:
    from agentscope.agent import Agent, ModelConfig, ReActConfig
    from agentscope.credential import OpenAICredential
    from agentscope.event import (
        ExceedMaxItersEvent,
        ModelCallEndEvent,
        ReplyEndEvent,
        RequireExternalExecutionEvent,
        RequireUserConfirmEvent,
        TextBlockDeltaEvent,
        TextBlockEndEvent,
        TextBlockStartEvent,
        ThinkingBlockDeltaEvent,
        ToolCallDeltaEvent,
        ToolCallEndEvent,
        ToolCallStartEvent,
        ToolResultEndEvent,
        ToolResultTextDeltaEvent,
    )
    from agentscope.message import UserMsg
    from agentscope.model import OpenAIChatModel, OpenAIResponseModel
    from agentscope.state import AgentState
    from agentscope.tool import Toolkit

    request_payload = {**request_payload, "run_id": run_id}
    max_iterations = _agent_loop_max_iterations()
    if checkpoint_json is None:
        confirmed = await _confirm_pending_position_draft(request_payload, emit)
        if confirmed is not None:
            return confirmed
    preflight_reply = _transaction_preflight_reply(request_payload)
    if preflight_reply:
        await emit({"type": "content_delta", "content": preflight_reply})
        await emit({"type": "content_end", "role": "answer"})
        return EngineOutcome(final_answer=preflight_reply)
    if checkpoint_json is None:
        staged_draft = await _stage_position_ocr_draft(request_payload, emit)
        if staged_draft is not None:
            return staged_draft
    system_prompt, schemas, write_names = _build_definition(request_payload)
    system_prompt = (
        f"{system_prompt.rstrip()}\n\n"
        "## AgentLoop 执行约束\n"
        f"- 本轮最多允许 {max_iterations} 次推理迭代；这是复杂任务预算，不是必须消耗的轮数。\n"
        "- 能直接回答时立即收敛，不得为消耗轮数继续调用工具。\n"
        "- 已获得的工具结果必须复用；不要使用完全相同的参数重复调用同一只读工具。\n"
        "- 工具返回的信息足以支持结论后，立即输出最终答案；证据不足则明确数据缺口。"
    )
    request_payload["write_tool_names"] = sorted(write_names)
    llm = active_llm_config(request_payload.get("model_config_id"))
    if not llm.get("base_url") or not llm.get("api_key"):
        raise RuntimeError("当前模型未配置 API Key 或 Base URL。")
    # Responses 模式由 AgentScope 映射为 reasoning.effort；Chat Completions
    # 模式继续沿用旧的 reasoning_effort / extra_body 线路。
    reasoning = reasoning_params(llm)
    wire_api = agent_llm_wire_api(llm)
    base_url = normalize_agent_base_url(str(llm["base_url"]))
    if wire_api == "responses":
        response_reasoning_effort = responses_reasoning_effort(llm, reasoning)
        parameters = OpenAIResponseModel.Parameters(
            temperature=float(llm.get("temperature", 0.2)),
            thinking_enable=bool(response_reasoning_effort),
            reasoning_effort=response_reasoning_effort,
        )
        model = OpenAIResponseModel(
            credential=OpenAICredential(api_key=llm["api_key"], base_url=base_url),
            model=llm["model"],
            parameters=parameters,
            stream=True,
            max_retries=2,
            retry_delay=1.0,
            client_kwargs={"timeout": 180.0},
        )
    else:
        parameters = OpenAIChatModel.Parameters(
            temperature=float(llm.get("temperature", 0.2)),
            thinking_enable=bool(reasoning["reasoning_effort"]),
            reasoning_effort=reasoning["reasoning_effort"],
        )
        extra_body = (
            {"enable_thinking": reasoning["enable_thinking"]}
            if reasoning["enable_thinking"] is not None
            else None
        )
        model = OpenAIChatModel(
            credential=OpenAICredential(api_key=llm["api_key"], base_url=base_url),
            model=llm["model"],
            parameters=parameters,
            stream=True,
            max_retries=2,
            retry_delay=1.0,
            client_kwargs={"timeout": 180.0},
            extra_body=extra_body,
        )
    state = AgentState.model_validate_json(checkpoint_json) if checkpoint_json else _history_state(request_payload)
    agent = Agent(
        name=str(request_payload.get("digital_human_code") or "ask_stock"),
        system_prompt=system_prompt,
        model=model,
        toolkit=Toolkit(tools=_build_tools(request_payload, schemas, write_names)),
        state=state,
        model_config=ModelConfig(max_retries=1),
        react_config=ReActConfig(max_iters=max_iterations),
    )

    if checkpoint_json:
        inputs, resolved_events = _resume_event(run_id, state)
        for item in resolved_events:
            await emit(item)
    else:
        inputs = UserMsg("user", materialize_agentscope_content(request_payload))

    await emit(
        {
            "type": "model_selected",
            "model": {
                "id": llm.get("id"),
                "display_name": llm.get("display_name"),
                "model_name": llm.get("model"),
                "provider_name": llm.get("provider_name"),
            },
        }
    )
    await emit(
        {
            "type": "stage_start",
            "stage": "agent_loop",
            "message": f"AgentScope 正在分析，最多 {max_iterations} 轮...",
            "max_iterations": max_iterations,
        }
    )
    outcome = EngineOutcome()
    text_buffer: list[str] = []
    text_block_open = False
    tool_names: dict[str, str] = {}
    tool_args: dict[str, list[str]] = {}
    tool_results: dict[str, list[str]] = {}
    waiting: list[dict[str, Any]] = []
    verified_schedule_receipts: list[dict[str, Any]] = []
    schedule_receipt_emitted = False
    transaction_agent = str(request_payload.get("digital_human_code") or "") in {
        "position-entry-agent",
        "watchlist-entry-agent",
    }
    reasoning_guard = ReasoningLoopGuard(
        repeat_limit=3 if transaction_agent else 5,
        max_chars=20_000 if transaction_agent else 120_000,
    )
    completed_business_write = False
    iteration_count = 0

    if state.context:
        for pending_call in state.context[-1].get_content_blocks("tool_call"):
            tool_names[pending_call.id] = pending_call.name
            tool_args[pending_call.id] = [pending_call.input or "{}"]

    async def emit_schedule_receipt() -> None:
        nonlocal schedule_receipt_emitted
        if schedule_receipt_emitted or not verified_schedule_receipts:
            return
        answer = _schedule_receipt_answer(verified_schedule_receipts)
        await emit({"type": "content_delta", "content": answer})
        await emit({"type": "content_end", "role": "answer"})
        outcome.final_answer = answer
        schedule_receipt_emitted = True

    async def close_text(role: str) -> None:
        nonlocal text_block_open
        if not text_block_open:
            return
        if role == "answer":
            if verified_schedule_receipts:
                await emit_schedule_receipt()
            else:
                await emit({"type": "content_end", "role": role})
                outcome.final_answer = "".join(text_buffer).strip()
        else:
            await emit({"type": "content_end", "role": role})
        text_buffer.clear()
        text_block_open = False

    async for event in agent.reply_stream(inputs):
        if await is_cancelled():
            outcome.status = "cancelled"
            break
        if isinstance(event, TextBlockStartEvent):
            text_block_open = True
        elif isinstance(event, TextBlockDeltaEvent):
            text_block_open = True
            text_buffer.append(event.delta)
            if not verified_schedule_receipts:
                await emit({"type": "content_delta", "content": event.delta})
        elif isinstance(event, TextBlockEndEvent):
            # The next event tells us whether this text precedes a tool call
            # (thought) or terminates the reply (answer).
            pass
        elif isinstance(event, ThinkingBlockDeltaEvent):
            loop_reason = reasoning_guard.observe(event.delta)
            if loop_reason:
                outcome.status = "failed"
                outcome.error = _reasoning_loop_error(
                    request_payload,
                    completed_business_write=completed_business_write,
                    transaction_agent=transaction_agent,
                )
                await emit(
                    {
                        "type": "agent_loop_guard_triggered",
                        "reason": loop_reason,
                        "reasoning_chars": reasoning_guard.total_chars,
                        "iteration": iteration_count,
                        "max_iterations": max_iterations,
                    }
                )
                break
            await emit({"type": "reasoning_delta", "content": event.delta})
        elif isinstance(event, ToolCallStartEvent):
            await close_text("thought")
            tool_names[event.tool_call_id] = event.tool_call_name
            tool_args[event.tool_call_id] = []
        elif isinstance(event, ToolCallDeltaEvent):
            tool_args.setdefault(event.tool_call_id, []).append(event.delta)
            await emit(
                {
                    "type": "tool_progress",
                    "tool_call_id": event.tool_call_id,
                    "tool": tool_names.get(event.tool_call_id, ""),
                    "display_name": _tool_display_name(tool_names.get(event.tool_call_id, "")),
                    "delta": event.delta,
                    "chars": sum(len(part) for part in tool_args.get(event.tool_call_id, [])),
                }
            )
        elif isinstance(event, ToolCallEndEvent):
            name = tool_names.get(event.tool_call_id, "")
            raw_args = "".join(tool_args.get(event.tool_call_id, []))
            try:
                parsed_args = json.loads(raw_args or "{}")
            except json.JSONDecodeError:
                parsed_args = {}
            canonical_args = json.dumps(
                parsed_args,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            risk_level = classify_tool_risk(name, parsed_args, write_names)
            await emit(
                {
                    "type": "action",
                    "tool_call_id": event.tool_call_id,
                    "tool": name,
                    "display_name": _tool_display_name(name),
                    "content": raw_args,
                    "arguments_sha256": hashlib.sha256(canonical_args.encode("utf-8")).hexdigest(),
                    "risk_level": risk_level,
                    "has_side_effects": risk_level in {"medium", "high"},
                }
            )
        elif isinstance(event, ToolResultTextDeltaEvent):
            tool_results.setdefault(event.tool_call_id, []).append(event.delta)
        elif isinstance(event, ToolResultEndEvent):
            name = tool_names.get(event.tool_call_id, "")
            raw_result = event.metadata.get("result") if isinstance(event.metadata, dict) else None
            execution_id = event.metadata.get("execution_id") if isinstance(event.metadata, dict) else None
            if not isinstance(raw_result, dict):
                text_result = "".join(tool_results.get(event.tool_call_id, []))
                try:
                    raw_result = json.loads(text_result)
                except (TypeError, json.JSONDecodeError):
                    raw_result = {"result": text_result}
            tool_succeeded = str(event.state) == "success" and not raw_result.get("error")
            if (
                name in write_names
                and tool_succeeded
                and int(raw_result.get("saved_count") or raw_result.get("changed_count") or 0) > 0
            ):
                completed_business_write = True
            await emit({"type": "observation", "content": _tool_preview(name, raw_result)})
            for artifact in raw_result.get("artifacts") or []:
                if isinstance(artifact, dict) and artifact.get("path"):
                    await emit({"type": "artifact", **artifact})
            tool_done_event = {
                "type": "tool_done",
                "tool_call_id": event.tool_call_id,
                "tool": name,
                "display_name": _tool_display_name(name),
                "success": tool_succeeded,
                "execution_id": execution_id,
            }
            if name in {"run_macd_backtest", "run_portfolio_backtest", "run_signal_backtest", "run_factor_backtest"} and raw_result.get("run_id"):
                tool_done_event["report_run_id"] = str(raw_result["run_id"])
                tool_done_event["report_url"] = str(
                    raw_result.get("report_url") or f"/backtests/{raw_result['run_id']}"
                )
            await emit(tool_done_event)
            await save_checkpoint(agent.state.model_dump_json(), None)
            if name in SCHEDULE_MUTATION_TOOL_NAMES:
                receipt, receipt_error = _verified_schedule_receipt(name, raw_result)
                if receipt_error:
                    outcome.status = "failed"
                    outcome.error = (
                        f"{_tool_display_name(name)}未成功，系统已禁止生成成功回执："
                        f"{receipt_error}"
                    )
                    await emit(
                        {
                            "type": "schedule_mutation_rejected",
                            "tool": name,
                            "execution_id": execution_id,
                            "reason": receipt_error,
                        }
                    )
                    break
                verified_schedule_receipts.append(receipt)
                completed_business_write = True
                # A verified schedule mutation has a complete server-owned
                # receipt. End the loop here so model prose can neither replace
                # the real job key nor turn a failed follow-up into ambiguity.
                break
            if raw_result.get("duplicate_tool_call_blocked"):
                outcome.status = "failed"
                outcome.error = (
                    "检测到相同工具和参数在本轮被重复调用，系统已自动停止本轮任务。"
                    "请复用已有结果，或调整查询条件后重试。"
                )
                await emit(
                    {
                        "type": "agent_loop_guard_triggered",
                        "reason": "duplicate_tool_call",
                        "tool": name,
                        "iteration": iteration_count,
                        "max_iterations": max_iterations,
                    }
                )
                break
        elif isinstance(event, RequireUserConfirmEvent):
            await close_text("thought")
            for tool_call in event.tool_calls:
                try:
                    args = json.loads(tool_call.input or "{}")
                except json.JSONDecodeError:
                    args = {}
                risk = classify_tool_risk(tool_call.name, args, write_names)
                approval = create_tool_approval(
                    session_id=int(request_payload["session_id"]),
                    user_id=int(request_payload["user_id"]),
                    tool_name=tool_call.name,
                    display_name=_tool_display_name(tool_call.name),
                    risk_level=risk,
                    args=args,
                    run_id=run_id,
                    tool_call_id=tool_call.id,
                )
                waiting.append({**approval, "tool_call_id": tool_call.id})
                await emit({"type": "approval_required", "tool_call_id": tool_call.id, **approval})
            await save_checkpoint(agent.state.model_dump_json(), waiting)
            outcome.status = "waiting_approval"
            outcome.waiting = waiting
        elif isinstance(event, RequireExternalExecutionEvent):
            from backend.app.agent_runtime.redis_broker import redis_broker
            from backend.app.agent_runtime.team_service import agent_team_service
            from backend.app.agent_runtime.workflow_service import agent_workflow_service

            await close_text("thought")
            for tool_call in event.tool_calls:
                if tool_call.name not in {"dispatch_agent_tasks", "run_agent_workflow", "create_agent_task_batch"}:
                    raise RuntimeError(f"不支持的外部编排工具：{tool_call.name}")
                try:
                    args = json.loads(tool_call.input or "{}")
                except json.JSONDecodeError as exc:
                    raise RuntimeError("Agent Team 参数不是有效 JSON。") from exc
                await save_checkpoint(
                    agent.state.model_dump_json(),
                    [{"type": "team", "tool_call_id": tool_call.id, "status": "creating"}],
                )
                if tool_call.name == "run_agent_workflow":
                    workflow = await asyncio.to_thread(
                        agent_workflow_service.start_workflow,
                        leader_run_id=run_id,
                        tool_call_id=tool_call.id,
                        arguments=args,
                    )
                    waiting.append(
                        {
                            "type": "workflow",
                            "workflow_run_id": workflow["workflow_run_id"],
                            "tool_call_id": tool_call.id,
                            "status": workflow["status"],
                            "deadline_at": workflow["deadline_at"],
                        }
                    )
                    await emit(
                        {
                            "type": "workflow_created",
                            "workflow_run_id": workflow["workflow_run_id"],
                            "template_code": workflow["template_code"],
                            "template_version": workflow["template_version"],
                            "objective": workflow["objective"],
                            "node_count": len(workflow["nodes"]),
                            "deadline_at": workflow["deadline_at"],
                        }
                    )
                    for node in workflow["nodes"]:
                        await emit(
                            {
                                "type": "workflow_node_status",
                                "workflow_run_id": workflow["workflow_run_id"],
                                "node_key": node["node_key"],
                                "role_name": node["role_name"],
                                "agent_code": node["agent_code"],
                                "status": node["status"],
                                "depends_on": node["depends_on"],
                                "child_run_id": node["child_run_id"],
                            }
                        )
                    for child_run_id in workflow.get("queued_run_ids") or []:
                        await redis_broker.enqueue(child_run_id)
                    continue
                if tool_call.name == "create_agent_task_batch":
                    from backend.app.quant.agent_board_service import TaskBatchSpec, TaskSpec, create_task_batch

                    tasks = [
                        TaskSpec(
                            action_key=str(t["action_key"]),
                            title=str(t["title"]),
                            agent_code=str(t["agent_code"]),
                            instruction=str(t["instruction"]),
                            expected_output=str(t.get("expected_output") or ""),
                            required=bool(t.get("required", True)),
                            priority=int(t.get("priority", 100)),
                        )
                        for t in args.get("tasks", [])
                    ]
                    batch = TaskBatchSpec(
                        objective=str(args.get("objective", "")),
                        execution_mode=str(args.get("execution_mode", "serial_items")),
                        completion_policy=str(args.get("completion_policy", "all_required")),
                        child_timeout_seconds=int(args.get("child_timeout_seconds", 600)),
                        tasks=tasks,
                    )
                    board_result = await asyncio.to_thread(
                        create_task_batch,
                        batch,
                        leader_run_id=run_id,
                        user_id=int(request_payload.get("user_id", 0)),
                        job_run_id=(
                            str(request_payload.get("source_ref") or "") or None
                            if request_payload.get("source_type") == "scheduler"
                            else None
                        ),
                        tool_call_id=tool_call.id,
                    )
                    waiting.append(
                        {
                            "type": "task_batch",
                            "team_id": board_result["team_id"],
                            "tool_call_id": tool_call.id,
                            "created_count": board_result["created_count"],
                            "status": "running",
                        }
                    )
                    await emit(
                        {
                            "type": "task_batch_created",
                            "team_id": board_result["team_id"],
                            "objective": batch.objective,
                            "created_count": board_result["created_count"],
                            "task_ids": board_result["task_ids"],
                        }
                    )
                    for task_id in board_result["task_ids"]:
                        await redis_broker.enqueue(task_id)
                    continue
                team = await asyncio.to_thread(
                    agent_team_service.start_team,
                    leader_run_id=run_id,
                    tool_call_id=tool_call.id,
                    arguments=args,
                )
                waiting_item = {
                    "type": "team",
                    "team_id": team["team_id"],
                    "tool_call_id": tool_call.id,
                    "status": team["status"],
                    "deadline_at": team["deadline_at"],
                }
                waiting.append(waiting_item)
                await emit(
                    {
                        "type": "team_created",
                        "team_id": team["team_id"],
                        "objective": team["objective"],
                        "member_count": len(team["members"]),
                        "rejected_count": len(team.get("rejected_tasks", [])),
                        **team.get("dispatch_stats", {}),
                        "deadline_at": team["deadline_at"],
                    }
                )
                for rejected in team.get("rejected_tasks", []):
                    await emit({"type": "team_member_rejected", "team_id": team["team_id"], **rejected})
                for member in team["members"]:
                    await emit(
                        {
                            "type": "team_member_queued",
                            "team_id": team["team_id"],
                            "child_run_id": member["run_id"],
                            "agent_code": member["agent_code"],
                            "agent_name": member["agent_name"],
                            "status": member["status"],
                        }
                    )
                    await redis_broker.enqueue(member["run_id"])
            await save_checkpoint(agent.state.model_dump_json(), waiting)
            outcome.status = "waiting_children"
            outcome.waiting = waiting
        elif isinstance(event, ModelCallEndEvent):
            iteration_count += 1
            await emit(
                {
                    "type": "agent_loop_progress",
                    "iteration": iteration_count,
                    "max_iterations": max_iterations,
                }
            )
            await save_checkpoint(agent.state.model_dump_json(), waiting or None)
        elif isinstance(event, ExceedMaxItersEvent):
            outcome.status = "failed"
            outcome.error = (
                f"Agent 已达到 {max_iterations} 次最大推理迭代上限，"
                "系统已停止本轮任务。请拆分任务或补充更明确的目标后重试。"
            )
            await emit(
                {
                    "type": "agent_loop_guard_triggered",
                    "reason": "max_iterations",
                    "iteration": max(iteration_count, max_iterations),
                    "max_iterations": max_iterations,
                }
            )
        elif isinstance(event, ReplyEndEvent):
            await close_text("answer")
            reason = str(event.finished_reason)
            if reason not in {"completed", "interrupted"}:
                outcome.status = "failed"
                error_info = getattr(event, "error", None)
                outcome.error = getattr(error_info, "message", None) or f"AgentScope ended: {reason}"

    if text_block_open:
        await close_text("thought" if waiting else "answer")
    if outcome.status == "completed" and verified_schedule_receipts:
        await emit_schedule_receipt()
    outcome.checkpoint_json = agent.state.model_dump_json()
    await save_checkpoint(outcome.checkpoint_json, waiting or None)
    if outcome.status == "completed":
        await emit(
            {
                "type": "stage_done",
                "stage": "agent_loop",
                "status": "completed",
                "iterations": iteration_count,
                "max_iterations": max_iterations,
            }
        )
    return outcome
