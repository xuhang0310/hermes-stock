from __future__ import annotations

import asyncio
import hashlib
import json
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
    TEAM_AND_SCHEDULE_TOOL_SCHEMAS,
    TOOL_DISPLAY_NAMES as ORCHESTRATION_DISPLAY_NAMES,
)
from backend.app.agent_runtime.payloads import materialize_agentscope_content
from backend.app.agent_runtime.tool_specs import should_externalize, tool_spec
from backend.app.agent_runtime.transaction_state import transaction_state_service
from backend.app.agents.tool_approval import (
    classify_tool_risk,
    create_tool_approval,
    requires_approval,
)
from backend.app.core.database import SessionLocal
from backend.app.core.llm_config import active_llm_config
from backend.app.core.models import AgentToolApproval
from backend.app.core.settings import settings
from backend.app.market.digital_human_tools import digital_human_tool_display_name, write_tool_schemas
from backend.app.market.stock_tools import (
    stock_observation_preview,
    stock_system_prompt_with_skills,
    stock_tool_display_name,
)
from backend.app.skills.registry import skill_registry


Emit = Callable[[dict[str, Any]], Awaitable[None]]
Checkpoint = Callable[[str, list[dict[str, Any]] | None], Awaitable[None]]
Cancelled = Callable[[], Awaitable[bool]]


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


def _tool_display_name(name: str) -> str:
    if name in ORCHESTRATION_DISPLAY_NAMES:
        return ORCHESTRATION_DISPLAY_NAMES[name]
    label = digital_human_tool_display_name(name)
    return label if label != name else stock_tool_display_name(name)


def _tool_preview(name: str, result: dict[str, Any]) -> str:
    if "error" in result:
        return str(result["error"])
    if name == "list_delegatable_agents":
        return f"可委派数字人 {len(result.get('agents', []))} 个。"
    if name == "dispatch_agent_tasks":
        return str(result.get("summary") or f"Agent Team {result.get('status', '已完成')}。")
    if name == "list_agent_schedules":
        return f"定时 Agent 任务 {len(result.get('jobs', []))} 个。"
    if name in SCHEDULE_MUTATION_TOOL_NAMES:
        job = result.get("job") if isinstance(result.get("job"), dict) else {}
        return f"定时任务操作完成：{job.get('name') or result.get('job_key') or '-'}。"
    from backend.app.market.digital_human_tools import digital_human_observation_preview

    preview = stock_observation_preview(name, result)
    return preview if preview != "工具调用完成。" else digital_human_observation_preview(name, result)


def _build_definition(request_payload: dict[str, Any]) -> tuple[str, list[dict], set[str]]:
    from backend.app.agents.stock_agent import TOOLS, _knowledge_system_prompt, _system_prompt

    chat_mode = str(request_payload.get("chat_mode") or "research")
    role_code = request_payload.get("digital_human_code")
    memory_context = str(request_payload.get("memory_context") or "")
    skills = list(request_payload.get("skills") or [])
    write_names: set[str] = set()
    if role_code == "position-entry-agent":
        from backend.app.agents.position_agent import POSITION_AGENT_TOOLS, _position_agent_prompt

        write_names = {"save_position_holdings"}
        return (
            _position_agent_prompt() + transaction_state_service.prompt_context(request_payload),
            list(POSITION_AGENT_TOOLS),
            write_names,
        )
    if role_code == "watchlist-entry-agent":
        from backend.app.agents.watchlist_agent import WATCHLIST_AGENT_TOOLS, _watchlist_agent_prompt

        write_names = {"add_watchlist_items", "remove_watchlist_items"}
        return (
            _watchlist_agent_prompt() + transaction_state_service.prompt_context(request_payload),
            list(WATCHLIST_AGENT_TOOLS),
            write_names,
        )
    if chat_mode == "stock":
        return (
            stock_system_prompt_with_skills(request_payload.get("digital_human_context"), skills, memory_context),
            skill_registry.get_tools_for("stock"),
            write_names,
        )
    if chat_mode == "knowledge":
        return _knowledge_system_prompt(memory_context), list(TOOLS), write_names

    tools = [*TOOLS, *skill_registry.get_tools_for("research", role_code=role_code)]
    if role_code == "stock-pm":
        tools.extend(TEAM_AND_SCHEDULE_TOOL_SCHEMAS)
        write_names.update(SCHEDULE_MUTATION_TOOL_NAMES)
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
    if role_code == "stock-pm":
        system_prompt += ORCHESTRATION_PROMPT
    active_artifacts = artifact_service.active_context(
        user_id=int(request_payload["user_id"]),
        session_id=int(request_payload["session_id"]) if request_payload.get("session_id") else None,
    )
    if active_artifacts:
        system_prompt += f"\n\n{active_artifacts}"
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
    role_code = str(request_payload.get("digital_human_code") or "")
    if role_code == "position-entry-agent":
        from backend.app.agents.position_agent import _execute_position_tool
        from backend.app.data.repository import StockRepository

        account_id = (transaction_state or {}).get("slots", {}).get("account_id")
        account_id = request_payload.get("default_account_id") or account_id
        with SessionLocal() as db:
            result = _execute_position_tool(
                repository=StockRepository(db),
                name=name,
                args=args,
                created_by=str(request_payload.get("user_label") or "agent"),
                source_name="AgentScope",
                image_mime_type=str(request_payload.get("image_mime_type") or "image/png"),
                image_sha256=request_payload.get("image_sha256"),
                account_id=int(account_id) if account_id else None,
            )
        transaction_state_service.after_tool(request_payload, name, args, result)
        return result
    if role_code == "watchlist-entry-agent":
        from backend.app.agents.watchlist_agent import _execute_watchlist_tool
        from backend.app.data.repository import StockRepository

        with SessionLocal() as db:
            result = _execute_watchlist_tool(
                StockRepository(db),
                name,
                args,
                created_by=str(request_payload.get("user_label") or "agent"),
            )
        transaction_state_service.after_tool(request_payload, name, args, result)
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
    if name in SCHEDULE_MUTATION_TOOL_NAMES or name == "list_agent_schedules":
        from backend.app.scheduler.schedule_service import agent_schedule_service

        return agent_schedule_service.execute(name, args, request_payload)
    if name in write_names:
        return _execute_position_write_tool(
            name,
            args,
            user_id=int(request_payload["user_id"]),
            user_label=str(request_payload.get("user_label") or "unknown"),
            default_account_id=request_payload.get("default_account_id"),
            digital_human_code=request_payload.get("digital_human_code"),
        )
    if skill_registry.has_tool(name):
        if name.startswith("ws_"):
            from backend.app.skills.agent_workspace import workspace_execution_scope, workspace_user_scope

            with workspace_user_scope(int(request_payload["user_id"])), workspace_execution_scope(
                bool(request_payload.get("allow_code_execution"))
            ):
                return skill_registry.execute(name, args)
        return skill_registry.execute(name, args)
    return {"error": f"未知工具 {name}"}


def _build_tools(request_payload: dict[str, Any], schemas: list[dict], write_names: set[str]):
    from agentscope.message import TextBlock, ToolResultState
    from agentscope.permission import PermissionBehavior, PermissionDecision
    from agentscope.tool import ToolBase, ToolChunk

    approval_policy = str(request_payload.get("approval_policy") or "ask")

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
            self.is_external_tool = self.name == "dispatch_agent_tasks"

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

    execution_results = []
    for tool_call in pending_calls:
        if str(tool_call.state) != "submitted":
            continue
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
                "type": "team_resumed",
                "team_id": result.get("team_id"),
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
    from agentscope.model import OpenAIChatModel
    from agentscope.state import AgentState
    from agentscope.tool import Toolkit

    request_payload = {**request_payload, "run_id": run_id}
    system_prompt, schemas, write_names = _build_definition(request_payload)
    request_payload["write_tool_names"] = sorted(write_names)
    llm = active_llm_config(request_payload.get("model_config_id"))
    if not llm.get("base_url") or not llm.get("api_key"):
        raise RuntimeError("当前模型未配置 API Key 或 Base URL。")
    parameters = OpenAIChatModel.Parameters(temperature=float(llm.get("temperature", 0.2)))
    model = OpenAIChatModel(
        credential=OpenAICredential(api_key=llm["api_key"], base_url=llm["base_url"]),
        model=llm["model"],
        parameters=parameters,
        stream=True,
        max_retries=2,
        retry_delay=1.0,
        client_kwargs={"timeout": 180.0},
    )
    state = AgentState.model_validate_json(checkpoint_json) if checkpoint_json else _history_state(request_payload)
    agent = Agent(
        name=str(request_payload.get("digital_human_code") or "ask_stock"),
        system_prompt=system_prompt,
        model=model,
        toolkit=Toolkit(tools=_build_tools(request_payload, schemas, write_names)),
        state=state,
        model_config=ModelConfig(max_retries=1),
        react_config=ReActConfig(max_iters=max(1, int(llm.get("max_steps") or 6))),
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
    await emit({"type": "stage_start", "stage": "agent_loop", "message": "AgentScope 正在分析..."})

    outcome = EngineOutcome()
    text_buffer: list[str] = []
    text_block_open = False
    tool_names: dict[str, str] = {}
    tool_args: dict[str, list[str]] = {}
    tool_results: dict[str, list[str]] = {}
    waiting: list[dict[str, Any]] = []

    if state.context:
        for pending_call in state.context[-1].get_content_blocks("tool_call"):
            tool_names[pending_call.id] = pending_call.name
            tool_args[pending_call.id] = [pending_call.input or "{}"]

    async def close_text(role: str) -> None:
        nonlocal text_block_open
        if not text_block_open:
            return
        await emit({"type": "content_end", "role": role})
        if role == "answer":
            outcome.final_answer = "".join(text_buffer).strip()
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
            await emit({"type": "content_delta", "content": event.delta})
        elif isinstance(event, TextBlockEndEvent):
            # The next event tells us whether this text precedes a tool call
            # (thought) or terminates the reply (answer).
            pass
        elif isinstance(event, ThinkingBlockDeltaEvent):
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
            await emit({"type": "observation", "content": _tool_preview(name, raw_result)})
            for artifact in raw_result.get("artifacts") or []:
                if isinstance(artifact, dict) and artifact.get("path"):
                    await emit({"type": "artifact", **artifact})
            await emit(
                {
                    "type": "tool_done",
                    "tool_call_id": event.tool_call_id,
                    "tool": name,
                    "display_name": _tool_display_name(name),
                    "success": str(event.state) == "success",
                    "execution_id": execution_id,
                }
            )
            await save_checkpoint(agent.state.model_dump_json(), None)
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

            await close_text("thought")
            for tool_call in event.tool_calls:
                if tool_call.name != "dispatch_agent_tasks":
                    raise RuntimeError(f"不支持的外部编排工具：{tool_call.name}")
                try:
                    args = json.loads(tool_call.input or "{}")
                except json.JSONDecodeError as exc:
                    raise RuntimeError("Agent Team 参数不是有效 JSON。") from exc
                await save_checkpoint(
                    agent.state.model_dump_json(),
                    [{"type": "team", "tool_call_id": tool_call.id, "status": "creating"}],
                )
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
            await save_checkpoint(agent.state.model_dump_json(), waiting or None)
        elif isinstance(event, ExceedMaxItersEvent):
            outcome.status = "failed"
            outcome.error = "Agent 达到最大推理轮数，请缩小任务范围后重试。"
        elif isinstance(event, ReplyEndEvent):
            await close_text("answer")
            reason = str(event.finished_reason)
            if reason not in {"completed", "interrupted"}:
                outcome.status = "failed"
                error_info = getattr(event, "error", None)
                outcome.error = getattr(error_info, "message", None) or f"AgentScope ended: {reason}"

    if text_block_open:
        await close_text("thought" if waiting else "answer")
    outcome.checkpoint_json = agent.state.model_dump_json()
    await save_checkpoint(outcome.checkpoint_json, waiting or None)
    if outcome.status == "completed":
        await emit({"type": "stage_done", "stage": "agent_loop", "status": "completed"})
    return outcome
