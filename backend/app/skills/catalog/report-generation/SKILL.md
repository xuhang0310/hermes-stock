---
name: report-generation
description: 将研究结果整理为专业 Markdown 报告、研究备忘录、事件点评、组合复盘或可进一步生成 Word/PDF/PPT 的内容结构。用户要求正式报告、研报、复盘文档、汇报材料或专业化输出时使用；修改已有文件应同时使用 workspace-artifact-editing。
compatibility: Hermes Stock workspace write tools; binary document generation may require Python and installed office libraries.
metadata:
  hermes-display-name: 专业报告生成
  hermes-category: artifact
  hermes-version: "3.0.0"
  hermes-risk-level: medium
allowed-tools: ws_write_file ws_edit_file ws_read_file ws_glob
---

# 专业报告生成

报告是证据和判断的可审计表达，不是把工具输出依次粘贴。先确定受众、用途和报告类型，再决定长度与结构。

## 报告类型

| 类型 | 核心用途 |
|---|---|
| 公司研究备忘录 | 建立或更新投资判断 |
| 事件点评 | 解释事实、传导和预期差 |
| 风险复盘 | 识别暴露、触发条件和行动边界 |
| 组合复盘 | 汇总集中度、归因和压力情景 |
| 回测报告 | 披露策略、基准、成本、结果和局限 |
| 管理层简报 | 用有限篇幅呈现结论、证据和决策事项 |

## 写作流程

1. 读取用户目标、现有研究结果和可用数据来源。
2. 明确读者、报告类型、数据截止日和篇幅。
3. 先写执行摘要和核心判断，再组织论据。
4. 每个重要判断至少对应一条可定位证据。
5. 展示最强反方证据、风险和数据缺口。
6. 表格统一单位、日期和实际/预测口径。
7. 写入工作区后检查路径、版本、哈希和格式。
8. 需要修改已有报告时读取原文件并做最小必要修改，不从头重建。

模板见 [references/report-templates.md](references/report-templates.md)。

## 表达纪律

- 即使是正式报告也要好读：标题写结论，术语第一次出现时解释一句，不输出证据等级和置信度标签；
- 结论强度与证据强度匹配；
- 不用“必然、确定、毫无风险”等绝对措辞；
- 不给主观概率伪造小数精度；
- 数据表注明单位和截止日期；
- 预测使用区间、情景和触发条件；
- 金融报告结尾保留研究用途和非投资建议声明。

## 交付

报告完成不只意味着产生文本。文件交付必须返回文件路径、版本、SHA-256、大小和验证状态。
