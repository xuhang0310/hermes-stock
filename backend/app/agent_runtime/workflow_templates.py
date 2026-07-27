from __future__ import annotations

from typing import Any


def _node(
    key: str,
    role: str,
    agent_code: str,
    instruction: str,
    expected_output: str,
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "role": role,
        "agent_code": agent_code,
        "instruction": instruction,
        "expected_output": expected_output,
        "depends_on": depends_on or [],
    }


BUILTIN_WORKFLOW_TEMPLATES: dict[str, dict[str, Any]] = {
    "quant_strategy_desk": {
        "name": "量化策略研究台",
        "description": "候选筛选与因子研究并行，随后进行可复现回测，最后由风控复核。",
        "category": "quant",
        "version": 1,
        "variables": {
            "target": {"required": True, "description": "标的、标的池或研究范围"},
            "market": {"required": False, "default": "A股", "description": "市场"},
            "period": {"required": False, "default": "近3年", "description": "研究和回测区间"},
        },
        "nodes": [
            _node(
                "screen",
                "候选筛选员",
                "research-analyst",
                "围绕 {target}（{market}）筛选适合量化检验的标的或交易条件。"
                "明确样本池、流动性过滤、停牌/ST/上市天数等约束，列出候选及可验证理由。",
                "候选池、过滤规则、数据范围、候选理由和数据缺口。",
            ),
            _node(
                "factor_research",
                "因子研究员",
                "quant-analyst",
                "围绕 {target}（{market}，{period}）研究可检验因子。"
                "至少覆盖趋势、反转、波动、成交量和基本面中的适用维度，说明公式、方向、衰减与避免未来函数的方法。",
                "因子定义、公式、数据口径、有效性假设、预期持有期和风险。",
            ),
            _node(
                "backtest",
                "策略回测员",
                "quant-analyst",
                "基于上游候选池与因子方案，对 {target} 在 {period} 内完成可复现回测。"
                "必须写明数据源、交易成本、滑点、调仓频率、基准、样本内外划分；"
                "输出收益、回撤、Sharpe、胜率、交易次数、稳定性和完整报告入口。",
                "可复现回测报告、参数、指标、交易明细摘要、稳健性检验和报告链接。",
                ["screen", "factor_research"],
            ),
            _node(
                "risk_audit",
                "量化风控复核官",
                "risk-officer",
                "审计上游量化策略和回测。检查样本量、过拟合、幸存者偏差、未来函数、"
                "流动性、容量、极端行情和参数敏感性，给出通过、限条件通过或不通过结论。",
                "风险评级、关键缺陷、压力测试结论、适用边界、上线前置条件。",
                ["backtest"],
            ),
        ],
    },
    "portfolio_review_board": {
        "name": "组合复盘委员会",
        "description": "归因、风险与执行质量并行审查，由组合研究负责人形成行动清单。",
        "category": "portfolio",
        "version": 1,
        "variables": {
            "target": {"required": False, "default": "当前全部持仓", "description": "组合或账户"},
            "period": {"required": False, "default": "本交易日", "description": "复盘区间"},
        },
        "nodes": [
            _node(
                "attribution",
                "收益归因分析师",
                "quant-analyst",
                "对 {target} 在 {period} 的收益和风险做归因。区分市场、行业、个股、仓位、"
                "择时和交易成本贡献，列出主要正负贡献项，缺数据时明确标记。",
                "组合收益归因表、Top贡献/拖累、基准对比、数据缺口。",
            ),
            _node(
                "portfolio_risk",
                "组合风险官",
                "risk-officer",
                "审查 {target} 在 {period} 的集中度、相关性、止损距离、回撤、事件风险和流动性。"
                "给出高优风险及触发条件，不得自动下单。",
                "风险热力表、风险等级、触发条件、仓位与止损建议。",
            ),
            _node(
                "execution_review",
                "执行质量审查员",
                "intelligence-officer",
                "复盘 {target} 在 {period} 的持仓变化、计划执行、事件响应和研究覆盖。"
                "比较计划与实际，识别遗漏、延迟、重复操作和待补证据。",
                "计划与实际差异、执行问题、研究缺口、待办清单。",
            ),
            _node(
                "cio_summary",
                "组合研究负责人",
                "research-analyst",
                "综合归因、风险与执行审查，为 {target} 形成复盘决议。"
                "按立即处理、下一交易日、继续观察分级，明确负责人、验收条件和证据缺口。",
                "组合复盘报告、优先级行动清单、观察项和未决风险。",
                ["attribution", "portfolio_risk", "execution_review"],
            ),
        ],
    },
    "investment_committee": {
        "name": "投资决策委员会",
        "description": "多头与空头论证并行，风险官交叉质询，最终输出有条件的投资决议。",
        "category": "decision",
        "version": 1,
        "variables": {
            "target": {"required": True, "description": "待决策标的"},
            "market": {"required": False, "default": "A股", "description": "市场"},
            "horizon": {"required": False, "default": "6-12个月", "description": "决策期限"},
        },
        "nodes": [
            _node(
                "bull_case",
                "多头研究员",
                "research-analyst",
                "为 {target}（{market}，{horizon}）建立最强多头论证。"
                "覆盖基本面、估值、催化剂、资金和可证伪条件，所有关键判断必须标记证据或缺口。",
                "多头论点、关键证据、目标区间、催化剂、证伪条件。",
            ),
            _node(
                "bear_case",
                "空头研究员",
                "risk-officer",
                "为 {target}（{market}，{horizon}）建立最强反方论证。"
                "主动寻找财务、估值、竞争、治理、政策和交易层面的失败路径。",
                "空头论点、风险证据、压力情景、下行空间、反驳多头所需证据。",
            ),
            _node(
                "cross_exam",
                "风险质询官",
                "risk-officer",
                "交叉审查 {target} 的多头与空头材料。识别事实冲突、共同盲点、"
                "证据质量问题和不可逆风险，并设计可执行的风险边界。",
                "争议矩阵、证据评级、共同盲点、仓位/止损/观察条件。",
                ["bull_case", "bear_case"],
            ),
            _node(
                "decision",
                "投资委员会主席",
                "macro-strategist",
                "基于完整辩论和风险质询，对 {target} 作出委员会决议。"
                "结论仅可为通过、限条件通过、观察或否决；写明期限、仓位上限、触发条件和复审时间。",
                "委员会决议、核心依据、执行边界、监控指标、复审条件。",
                ["cross_exam"],
            ),
        ],
    },
    "fundamental_research_team": {
        "name": "基本面深度研究组",
        "description": "财务、估值和商业质量三线并行，最终形成证据驱动的深度研究报告。",
        "category": "fundamental",
        "version": 1,
        "variables": {
            "target": {"required": True, "description": "公司代码或名称"},
            "market": {"required": False, "default": "A股", "description": "市场"},
            "period": {"required": False, "default": "近3-5年", "description": "财务观察期"},
        },
        "nodes": [
            _node(
                "financials",
                "财务分析师",
                "quant-analyst",
                "分析 {target}（{market}）在 {period} 的利润表、资产负债表和现金流。"
                "重点核验盈利质量、应收存货、债务、资本开支、自由现金流和异常变动。",
                "核心财务指标趋势、盈利质量判断、异常项、同业对比和风险。",
            ),
            _node(
                "valuation",
                "估值分析师",
                "research-analyst",
                "对 {target}（{market}）进行多方法估值。至少包含历史分位、可比公司和"
                "适用的绝对估值，披露关键假设、敏感性与安全边际。",
                "估值矩阵、关键假设、悲观/基准/乐观区间、安全边际和催化剂。",
            ),
            _node(
                "quality",
                "商业质量分析师",
                "intelligence-officer",
                "评估 {target} 的商业模式、竞争格局、护城河、管理层资本配置和治理质量。"
                "关注市场份额变化、替代风险、关联交易与信息披露质量。",
                "商业质量评分、护城河证据、管理层评价、竞争风险和长期持有条件。",
            ),
            _node(
                "report",
                "深度报告主笔",
                "research-analyst",
                "整合 {target} 的财务、估值和商业质量研究，校验冲突并形成深度报告。"
                "必须区分事实、推断和未知，数据暂不可用时在报告中明确说明局限。",
                "评级与目标区间、核心逻辑、财务/估值/质量章节、风险、催化剂和数据来源。",
                ["financials", "valuation", "quality"],
            ),
        ],
    },
    "risk_committee": {
        "name": "风险委员会",
        "description": "回撤、尾部风险和市场状态并行评估，由首席风控形成统一处置意见。",
        "category": "risk",
        "version": 1,
        "variables": {
            "target": {"required": True, "description": "标的、组合或策略"},
            "period": {"required": False, "default": "近3年", "description": "风险观察期"},
        },
        "nodes": [
            _node(
                "drawdown",
                "回撤分析师",
                "quant-analyst",
                "分析 {target} 在 {period} 的历史回撤、持续时间、修复周期和当前离高点距离。"
                "列出主要回撤事件与波动、趋势、成交量背景。",
                "回撤事件表、频率分布、修复时间、当前回撤状态和预警阈值。",
            ),
            _node(
                "tail_risk",
                "尾部风险分析师",
                "risk-officer",
                "评估 {target} 的尾部风险。给出适用口径的 VaR/CVaR 或替代压力指标，"
                "至少设计三个历史或假设压力情景，并说明模型限制。",
                "尾部风险指标、压力测试、极端损失路径、保护方案和模型限制。",
            ),
            _node(
                "regime",
                "市场状态分析师",
                "macro-strategist",
                "判断 {target} 当前所处的市场状态。综合趋势、波动、流动性、政策和行业周期，"
                "给出牛市/熊市/震荡概率及状态切换领先指标。",
                "当前状态、置信度、相似历史阶段、切换信号和未来情景概率。",
            ),
            _node(
                "risk_signoff",
                "首席风控官",
                "risk-officer",
                "整合 {target} 的回撤、尾部风险和市场状态研究。交叉检查冲突，"
                "给出低/中/高/极高风险评级及仓位、止损、对冲和复审建议。",
                "统一风险评级、核心风险、行动项、触发阈值、复审时间和未覆盖风险。",
                ["drawdown", "tail_risk", "regime"],
            ),
        ],
    },
}
