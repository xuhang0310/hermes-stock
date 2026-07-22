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


TEAM_AND_SCHEDULE_TOOL_SCHEMAS = [
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
        "查看当前用户通过总调度创建的定时任务。",
        {
            "properties": {
                "include_disabled": {"type": "boolean", "default": True},
            },
            "required": [],
        },
    ),
    _tool(
        "create_agent_schedule",
        "创建持久化定时 Agent 任务，支持一次、每日、每周、月末和间隔执行；可让情报官定时查新闻，也可授权工作区沙箱代码任务。",
        {
            "properties": {
                "name": {"type": "string", "description": "任务名称。"},
                "target_agent_code": {"type": "string", "description": "执行该任务的数字人 code。"},
                "instruction": {"type": "string", "description": "每次触发时交给数字人的完整指令。"},
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
        "修改当前用户通过总调度创建的定时任务。只传需要变更的字段。",
        {
            "properties": {
                "job_key": {"type": "string"},
                "name": {"type": "string"},
                "target_agent_code": {"type": "string"},
                "instruction": {"type": "string"},
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
]


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

总调度编排规则：
1. 简单问题直接回答；只有跨专业、可并行且需要汇总证据的复杂任务才创建 Agent Team。
2. 委派前先调用 list_delegatable_agents，必须逐字复制返回的 agent_code，禁止翻译、改成下划线或自行创造代码。一轮调度中每个 agent_code 只能出现一次；同一数字人的多个工作要求必须合并为一个编号工作包，禁止为不同股票或不同步骤重复创建同一数字人的子任务。最多使用 20 个不同数字人。
3. 持仓新增、修改和截图录入委派给 position-entry-agent；自选查询、增加和删除委派给 watchlist-entry-agent。这两个专用 Agent 只有各自业务写权限，不具备代码执行权限。
4. dispatch_agent_tasks 返回后，根据每个成员的状态、证据和缺口统一复核并统稿；部分失败时明确标注，不得把失败结果当成事实。
5. 用户明确要求“定时、每天、每周、稍后、每隔多久”执行时，可以创建定时任务。创建前复述执行者、时间、指令、是否推送飞书、是否允许代码执行。
6. 定时代码只在用户隔离工作区的 Docker 沙箱执行；不要把密钥、数据库密码或宿主机路径写进任务指令。
7. 修改、暂停、恢复和删除任务时，先用 list_agent_schedules 找到准确 job_key。
"""


TOOL_DISPLAY_NAMES = {
    "list_delegatable_agents": "查看可委派数字人",
    "dispatch_agent_tasks": "派发 Agent Team",
    "list_agent_schedules": "查看定时 Agent 任务",
    "create_agent_schedule": "创建定时 Agent 任务",
    "update_agent_schedule": "修改定时 Agent 任务",
    "pause_agent_schedule": "暂停或恢复定时 Agent 任务",
    "delete_agent_schedule": "删除定时 Agent 任务",
}
