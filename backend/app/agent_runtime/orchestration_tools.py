from __future__ import annotations

from typing import Any


def _tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "additionalProperties": False, **parameters},
        },
    }


# 定时任务指令会原样交给数字人，指令里的规矩常被它复述进推送（见 docs/scheduled-task-prompts.md）。
_SCHEDULE_INSTRUCTION_GUIDE = (
    "每次触发时交给数字人的完整指令。写法：1) 贴着用户的原话写清要交付什么、几点前要，"
    "交付物要“先给结论、大白话、能照着做”，用户要具体价格和数量就要求每条都给出；"
    "2) 只写用户这次要的内容，不要把以前对话里的一次性约定、或你自己加的限制（预算比例、"
    "减仓上限、数据核实前不给数量等）写成每次都要守的规矩；3) 不写“推送飞书”，"
    "push_to_feishu 打开后由系统自动推送；4) 不写核对清单和免责条款，尽量控制在 800 字以内。"
)

TEAM_AND_SCHEDULE_TOOL_SCHEMAS = [
    _tool(
        "list_workflow_templates",
        "列出系统内置、已审核的 Swarm DAG 工作流模板、变量、角色和依赖关系。"
        "当任务需要固定研究流程、阶段依赖、风控签核或可恢复执行时，先调用本工具。",
        {"properties": {}, "required": []},
    ),
    _tool(
        "run_agent_workflow",
        "启动一个持久化 Swarm DAG 工作流。工作流节点按依赖自动解锁，同层并行执行，"
        "失败会阻断下游，所有节点都保留独立数字人会话。调用后总调度挂起，工作流结束后自动恢复。",
        {
            "properties": {
                "template_code": {
                    "type": "string",
                    "enum": [
                        "quant_strategy_desk",
                        "portfolio_review_board",
                        "investment_committee",
                        "fundamental_research_team",
                        "risk_committee",
                    ],
                    "description": "必须使用 list_workflow_templates 返回的模板 code。",
                },
                "objective": {"type": "string", "description": "本次工作流要完成的总目标。"},
                "variables": {
                    "type": "object",
                    "description": "模板变量，例如 target、market、period、horizon。",
                    "additionalProperties": {
                        "type": ["string", "number", "boolean"],
                    },
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 300,
                    "maximum": 7200,
                    "default": 1800,
                },
            },
            "required": ["template_code", "objective", "variables"],
        },
    ),
    _tool(
        "list_delegatable_agents",
        "列出总调度当前可以委派的数字人、职责和只读限制。拆分复杂任务前先调用。",
        {"properties": {}, "required": []},
    ),
    _tool(
        "dispatch_agent_tasks",
        "把一个复杂任务并行委派给多个专业数字人。同一轮每个 agent_code 只能出现一次；同一数字人的多个要求必须合并到一条 instruction 中。调用后总调度会挂起，所有子任务完成或超时后自动恢复并收到结构化结果。",
        {
            "properties": {
                "objective": {"type": "string", "description": "本次协作要解决的总目标。"},
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "agent_code": {
                                "type": "string",
                                "description": (
                                    "必须原样使用 list_delegatable_agents 返回的 code，不要翻译或改写。"
                                    "内置示例：research-analyst、position-entry-agent、"
                                    "watchlist-entry-agent。"
                                ),
                            },
                            "instruction": {"type": "string", "description": "边界明确、可独立完成的子任务。"},
                            "expected_output": {"type": "string", "description": "希望返回的结构或验收条件。"},
                            "required": {"type": "boolean", "default": True},
                        },
                        "required": ["agent_code", "instruction"],
                    },
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 60,
                    "maximum": 1800,
                    "description": "整个 Team 的截止时间，默认 600 秒。",
                },
            },
            "required": ["objective", "tasks"],
        },
    ),
    _tool(
        "list_agent_schedules",
        "查询 scheduled_jobs 中当前账号有权查看的真实定时任务，并返回总数、启用数、状态、"
        "执行数字人和调度配置。超级管理员/管理员可查看全部任务；普通用户可查看自己的任务和"
        "系统公共任务。凡是用户询问当前任务数量、任务状态或已有配置，必须先调用本工具，"
        "禁止根据会话内容猜测。",
        {
            "properties": {
                "include_disabled": {"type": "boolean", "default": True},
            },
            "required": [],
        },
    ),
    _tool(
        "get_agent_schedule",
        "按 job_key 查询一条真实定时任务的完整配置。修改任务前应先调用 list_agent_schedules "
        "定位 job_key，再调用本工具核对当前值，避免覆盖未要求变更的字段。",
        {
            "properties": {
                "job_key": {"type": "string", "description": "定时任务唯一标识。"},
            },
            "required": ["job_key"],
        },
    ),
    _tool(
        "create_agent_schedule",
        "创建持久化定时 Agent 任务，支持一次、每日、每周、月末和间隔执行；"
        "可让情报官定时查新闻，也可授权工作区沙箱代码任务。只有工具返回 verified=true "
        "才代表创建成功，最终任务编号必须逐字使用工具返回的 job.job_key。",
        {
            "properties": {
                "name": {"type": "string", "description": "任务名称。"},
                "target_agent_code": {
                    "type": "string",
                    "description": (
                        "执行该任务的数字人 code。总调度必须使用 stock-pm；"
                        "兼容 orchestrator、pm 和中文名，但应优先使用真实 code。"
                    ),
                },
                "instruction": {"type": "string", "description": _SCHEDULE_INSTRUCTION_GUIDE},
                "schedule": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["once", "daily", "weekly", "monthly_last", "interval"],
                        },
                        "run_at": {"type": "string", "description": "一次任务的本地时间，ISO 8601。"},
                        "run_time": {"type": "string", "description": "每日/每周/月末的 HH:MM。"},
                        "weekdays": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 1, "maximum": 7},
                            "description": "ISO 星期，1=周一，7=周日。",
                        },
                        "interval_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
                        "window_start": {"type": "string", "description": "间隔任务开始时间 HH:MM。"},
                        "window_end": {"type": "string", "description": "间隔任务结束时间 HH:MM。"},
                    },
                    "required": ["type"],
                },
                "trading_day_only": {"type": "boolean", "default": False},
                "push_to_feishu": {"type": "boolean", "default": False},
                "allow_code_execution": {
                    "type": "boolean",
                    "default": False,
                    "description": "允许任务在当前用户隔离工作区的 Docker 沙箱执行 Python。",
                },
            },
            "required": ["name", "target_agent_code", "instruction", "schedule"],
        },
    ),
    _tool(
        "update_agent_schedule",
        "修改当前账号有权管理的真实定时任务。管理员可修改全部任务，普通用户只能修改自己"
        "通过 Agent 创建的任务。只传用户明确要求变更的字段，其他字段保持不变。",
        {
            "properties": {
                "job_key": {"type": "string"},
                "name": {"type": "string"},
                "target_agent_code": {"type": "string"},
                "instruction": {"type": "string", "description": _SCHEDULE_INSTRUCTION_GUIDE},
                "schedule": {"type": "object"},
                "trading_day_only": {"type": "boolean"},
                "push_to_feishu": {"type": "boolean"},
                "allow_code_execution": {"type": "boolean"},
                "enabled": {"type": "boolean"},
            },
            "required": ["job_key"],
        },
    ),
    _tool(
        "pause_agent_schedule",
        "暂停或恢复当前用户通过总调度创建的定时任务。",
        {
            "properties": {
                "job_key": {"type": "string"},
                "enabled": {"type": "boolean", "description": "false 暂停，true 恢复。"},
            },
            "required": ["job_key", "enabled"],
        },
    ),
    _tool(
        "delete_agent_schedule",
        "删除当前用户通过总调度创建的定时任务，历史运行记录仍保留审计。",
        {
            "properties": {"job_key": {"type": "string"}},
            "required": ["job_key"],
        },
    ),
    _tool(
        "create_agent_task_batch",
        "当报告或分析产生需要其他数字人继续完成的动作时，必须调用本工具以结构化参数提交任务批次。"
        "禁止只在正文中输出'待完成'。调用后总调度挂起，所有子任务完成或超时后自动恢复。",
        {
            "properties": {
                "objective": {
                    "type": "string",
                    "description": "本批次任务要完成的总目标。",
                },
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "action_key": {
                                "type": "string",
                                "description": "本批次内唯一标识，格式建议：动作-标的-日期，如 verify-600984-20260801。",
                            },
                            "title": {
                                "type": "string",
                                "description": "看板卡片标题，简明扼要。",
                            },
                            "agent_code": {
                                "type": "string",
                                "description": (
                                    "必须原样使用 list_delegatable_agents 返回的 code。"
                                    "同一轮每个 agent_code 可出现多次（串行执行）。"
                                ),
                            },
                            "instruction": {
                                "type": "string",
                                "description": "边界明确、可独立完成的子任务指令。",
                            },
                            "expected_output": {
                                "type": "string",
                                "description": "希望返回的结构或验收条件。",
                            },
                            "required": {
                                "type": "boolean",
                                "default": True,
                                "description": "是否为必选任务。",
                            },
                            "priority": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 999,
                                "default": 100,
                                "description": "优先级，数值越大越先执行。默认 100。",
                            },
                        },
                        "required": ["action_key", "title", "agent_code", "instruction"],
                    },
                },
                "execution_mode": {
                    "type": "string",
                    "enum": ["serial_items", "merge_by_agent"],
                    "default": "serial_items",
                    "description": "serial_items：每个动作独立执行；merge_by_agent：同一 Agent 的任务合并。",
                },
                "completion_policy": {
                    "type": "string",
                    "enum": ["all_required", "best_effort"],
                    "default": "all_required",
                    "description": "all_required：必选任务全部成功才算成功；best_effort：到期后汇总已有结果。",
                },
                "child_timeout_seconds": {
                    "type": "integer",
                    "minimum": 60,
                    "maximum": 3600,
                    "default": 600,
                    "description": "整个批次的截止时间，默认 600 秒。",
                },
            },
            "required": ["objective", "tasks"],
        },
    ),
]


TASK_HANDOFF_TOOL_NAMES = frozenset(
    {"list_delegatable_agents", "create_agent_task_batch"}
)
TASK_HANDOFF_TOOL_SCHEMAS = tuple(
    schema
    for schema in TEAM_AND_SCHEDULE_TOOL_SCHEMAS
    if schema["function"]["name"] in TASK_HANDOFF_TOOL_NAMES
)
TASK_HANDOFF_PROMPT = """

任务转交规则：
1. 当报告或分析产生需要其他数字人继续完成的动作时，禁止只在正文中输出“待完成”。
2. 先调用 list_delegatable_agents，并逐字使用返回的 agent_code。
3. 必须调用 create_agent_task_batch 提交任务。每个任务都要有唯一的 action_key、明确的 agent_code 和可独立完成的 instruction。
4. 只创建报告确实要求继续执行的任务，不要把普通建议或观察项转成任务。
"""


# 网页/飞书对话里用户在等回复。生产上总调度把“还能上 90 吗”“持仓怎么样”这类问题都交给工作流，
# 一次 10–36 分钟；实时对话默认自己直接答，工作流只在用户点名要多角色深度研究时启动。
INTERACTIVE_ORCHESTRATION_PROMPT = """

实时对话规则（当前是用户在对话框里提问、正在等你回复；与上面的 Swarm 工作流规则冲突时以这里为准）：
1. 单只股票分析、能不能买/补仓/拿多久、持仓今天怎么样、某个事件有什么影响这类日常问题，用你自己的工具直接回答，不要启动工作流、Agent Team 或看板任务。
2. 只有用户明确要求“委员会/多角色/全面深度研究/组合复盘报告/风险审计”时，才调用 list_workflow_templates 和 run_agent_workflow；启动前先告诉用户大概要等 10 分钟以上。
3. 直接回答后，如果问题确实值得多角色深挖，可以在结尾用一句话提议，例如“需要的话我可以召集投资决策委员会做一次多角色评审，大约 10–15 分钟”。
"""


ORCHESTRATION_TOOL_NAMES = frozenset(
    schema["function"]["name"] for schema in TEAM_AND_SCHEDULE_TOOL_SCHEMAS
)
SCHEDULE_MUTATION_TOOL_NAMES = frozenset(
    {
        "create_agent_schedule",
        "update_agent_schedule",
        "pause_agent_schedule",
        "delete_agent_schedule",
    }
)


ORCHESTRATION_PROMPT = """

Swarm 工作流规则：
- 对量化策略、组合复盘、投资决策、基本面深研或风险审计，先调用 list_workflow_templates，
  命中模板时优先使用 run_agent_workflow。
- Swarm 节点和依赖由模板决定，不要再把同一模板重复拆成动态 Agent Team。
- 只有临时、动态、无法套用标准模板的并行协作才使用 dispatch_agent_tasks。

总调度编排规则：
1. 简单问题直接回答；只有跨专业、可并行且需要汇总证据的复杂任务才创建 Agent Team。
2. 委派前先调用 list_delegatable_agents，必须逐字复制返回的 agent_code，禁止翻译、改成下划线或自行创造代码。一轮调度中每个 agent_code 只能出现一次；同一数字人的多个工作要求必须合并为一个编号工作包，禁止为不同股票或不同步骤重复创建同一数字人的子任务。最多使用 20 个不同数字人。
3. 持仓新增、修改和截图录入委派给 position-entry-agent；自选查询、增加和删除委派给 watchlist-entry-agent。这两个专用 Agent 只有各自业务写权限，不具备代码执行权限。
4. dispatch_agent_tasks 返回后，根据每个成员的状态、证据和缺口统一复核并统稿；部分失败时明确标注，不得把失败结果当成事实。
5. 用户明确要求“定时、每天、每周、稍后、每隔多久”执行时，可以创建定时任务。创建前复述执行者、时间、指令、是否推送飞书、是否允许代码执行。
6. 定时代码只在用户隔离工作区的 Docker 沙箱执行；不要把密钥、数据库密码或宿主机路径写进任务指令。
7. 用户询问“有多少定时任务、有哪些任务、是否启用、最近是否成功”时，必须调用 list_agent_schedules 查询真实数据，禁止用会话记忆推断，更不能把未查询解释成 0。
8. 修改、暂停、恢复和删除任务时，先用 list_agent_schedules 找到准确 job_key，再用 get_agent_schedule 核对当前配置；update_agent_schedule 只提交需要变化的字段。
9. 当报告或分析产生需要其他数字人继续完成的动作时，禁止只在正文中输出"待完成"。必须调用 create_agent_task_batch，以结构化参数提交任务批次。每个任务必须有唯一的 action_key、明确的 agent_code 和可独立完成的 instruction。
10. 定时任务写操作只有在工具返回 verified=true 时才算成功。任务编号只能逐字引用工具返回的 job.job_key 或 job_key；工具报错、结果不确定或未返回核验标记时，严禁声称“已创建”“已生效”或自行生成任务编号。
"""


TOOL_DISPLAY_NAMES = {
    "list_workflow_templates": "查看 Swarm 工作流模板",
    "run_agent_workflow": "运行 Swarm 工作流",
    "list_delegatable_agents": "查看可委派数字人",
    "dispatch_agent_tasks": "派发 Agent Team",
    "list_agent_schedules": "查看定时 Agent 任务",
    "get_agent_schedule": "查看定时任务详情",
    "create_agent_schedule": "创建定时 Agent 任务",
    "update_agent_schedule": "修改定时 Agent 任务",
    "pause_agent_schedule": "暂停或恢复定时 Agent 任务",
    "delete_agent_schedule": "删除定时 Agent 任务",
    "create_agent_task_batch": "创建看板任务批次",
}
