"""Swarm 团队模板 —— 预定义的多智能体协作配置。

每个模板定义一组数字人角色组合和协作目标，前端通过 ``/api/agent/swarm-teams`` 获取。
用户在对话框输入 ``//`` 触发 swarm 选择菜单。
"""
from __future__ import annotations


SWARM_TEAMS: list[dict] = [
    {
        "id": "full-research",
        "name": "全面研究团队",
        "description": "研究员 + 情报官 + 风控官 + 量化分析师 联合深度分析",
        "icon": "🔬",
        "members": [
            {"role": "research-analyst", "label": "研究员", "task": "基本面与技术面综合分析"},
            {"role": "intelligence-officer", "label": "情报官", "task": "搜集新闻、公告、行业情报"},
            {"role": "risk-officer", "label": "风控官", "task": "风险评估与止损建议"},
            {"role": "quant-analyst", "label": "量化分析师", "task": "策略回测与数据验证"},
        ],
        "objective_template": "对 {symbol} 进行全面研究：基本面+技术面+消息面+风险+回测",
        "tags": ["深度分析", "个股研究"],
    },
    {
        "id": "risk-review",
        "name": "风控审查团队",
        "description": "风控官 + 研究员 + 情报官 联合审查持仓风险",
        "icon": "🛡️",
        "members": [
            {"role": "risk-officer", "label": "风控官", "task": "持仓风险评估与预警"},
            {"role": "research-analyst", "label": "研究员", "task": "持仓标的基本面复查"},
            {"role": "intelligence-officer", "label": "情报官", "task": "负面消息与风险事件排查"},
        ],
        "objective_template": "审查当前持仓风险：逐标的检查基本面变化、负面消息、技术破位风险",
        "tags": ["风控", "持仓审查"],
    },
    {
        "id": "stock-pick",
        "name": "选股团队",
        "description": "情报官 + 研究员 + 量化分析师 协作筛选标的",
        "icon": "🎯",
        "members": [
            {"role": "intelligence-officer", "label": "情报官", "task": "搜集热点板块与催化事件"},
            {"role": "research-analyst", "label": "研究员", "task": "候选标的基本面筛选"},
            {"role": "quant-analyst", "label": "量化分析师", "task": "策略回测验证选股逻辑"},
        ],
        "objective_template": "在 {sector} 板块中筛选值得关注的标的：消息催化+基本面+回测验证",
        "tags": ["选股", "板块"],
    },
    {
        "id": "position-review",
        "name": "持仓复盘团队",
        "description": "研究员 + 风控官 + 量化分析师 复盘持仓表现",
        "icon": "📊",
        "members": [
            {"role": "research-analyst", "label": "研究员", "task": "持仓标的涨跌归因"},
            {"role": "risk-officer", "label": "风控官", "task": "仓位合理性与集中度评估"},
            {"role": "quant-analyst", "label": "量化分析师", "task": "历史策略表现复盘"},
        ],
        "objective_template": "复盘当前持仓表现：盈亏归因+仓位评估+策略有效性检验",
        "tags": ["复盘", "持仓"],
    },
    {
        "id": "event-response",
        "name": "事件响应团队",
        "description": "情报官 + 研究员 + 风控官 快速响应突发事件",
        "icon": "⚡",
        "members": [
            {"role": "intelligence-officer", "label": "情报官", "task": "事件信息搜集与核实"},
            {"role": "research-analyst", "label": "研究员", "task": "影响评估与受益/受损分析"},
            {"role": "risk-officer", "label": "风控官", "task": "应急操作建议"},
        ],
        "objective_template": "快速响应事件 [{event}]：信息核实+影响评估+操作建议",
        "tags": ["事件", "应急"],
    },
]


def list_swarm_teams() -> list[dict]:
    """返回全部 swarm 团队模板。"""
    return [
        {
            **team,
            "member_count": len(team["members"]),
            "member_roles": [m["role"] for m in team["members"]],
        }
        for team in SWARM_TEAMS
    ]


def get_swarm_team(team_id: str) -> dict | None:
    """按 id 查找单个团队模板。"""
    for team in SWARM_TEAMS:
        if team["id"] == team_id:
            return {
                **team,
                "member_count": len(team["members"]),
                "member_roles": [m["role"] for m in team["members"]],
            }
    return None
