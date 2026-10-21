---
name: position-risk-review
description: 基于系统真实持仓账户分析成本、仓位、盈亏、止损距离、集中度、事件风险和行动边界。用户上传持仓截图、询问持仓怎么办、是否补仓减仓、账户风险、止损或要求持仓复盘时使用。
compatibility: Requires authenticated access to the user's Hermes Stock portfolio records.
metadata:
  hermes-display-name: 持仓风险复盘
  hermes-category: portfolio
  hermes-version: "3.0.0"
  hermes-risk-level: high
allowed-tools: get_portfolio_snapshot get_risk_context check_stop_loss_status
---

# 持仓风险复盘

持仓问题必须先确认账户和真实数据。不能从聊天印象虚构数量、成本、盈亏或证券账户。

## 上下文确认

1. 优先读取当前对话已确认的账户、截图识别结果和待提交草稿。
2. 用户刚刚选择了账户或补充了信息时，继承该上下文，不重复追问。
3. 信息存在冲突时展示冲突字段，只询问真正阻塞保存或分析的部分。
4. 研究型 Agent 不得直接修改持仓；写入动作交给持仓录入 Agent 并遵守审批。

## 风险复盘

- 当前数量、成本、价格和仓位；
- 单票占账户或组合比例；
- 距止损线、关键支撑和风险阈值的距离；
- 浮盈浮亏对决策造成的锚定偏差；
- 近期公告、事件和流动性风险；
- 补仓是否会扩大集中度或相关风险；
- 继续持有、观察、减仓等情景的触发条件。

详细行动边界见 [references/risk-policy.md](references/risk-policy.md)。

## 回答要讲到

用白话结论做小标题（如“这只票已经占了你一半仓位”），不照抄下面的条目：

- 你的持仓现在怎么样：数量、成本、现价、盈亏、仓位占比；
- 最大的风险是什么、离止损还有多远；
- 不同情况怎么应对，写成“如果……就……”；
- 现在最好不要做什么；
- 还需要用户补充什么。

建议是研究支持，不是自动交易指令。没有账户级事实时只做假设性讨论，并明确假设。
