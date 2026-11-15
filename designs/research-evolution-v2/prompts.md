# ImageGen 最终提示词 · 研究进化中心 V2

生成日期：2026-09-19。模式：内置 ImageGen（未使用 CLI/API 回退）。用途：静态 UI 评审，所有数据均为示例。

三张图片为生成的位图原型，不是已经实现的页面。第 2、3 张使用第 1 张作为视觉风格参考；未修改生成图中的像素。

## 1. 01-daily-research.png

```text
Use case: ui-mockup.
Generate a NEW redesigned high fidelity Chinese enterprise research web app screen, not a revision of the previous crowded dashboard. ONE flat desktop screenshot, 2560x1600 landscape (16:10), crisp readable simplified Chinese, no device mockup or presentation frame. Product "SixRabbits Stock". Screen 1 of a coherent 3-screen design system: "每日研究台".
Design system: elegant matte near-black #0c0e11 background; charcoal #15181d surfaces; thin grey #2b3037 dividers; ivory #f0eee7 text; champagne-gold #d5b46a only for selection and main action; muted jade #65bda8 for completed tasks, coral #e2807b for failures, amber for running, slate for waiting. Precise Chinese sans-serif, distinct monospaced IDs, generous 24px padding, 8px corners, no glowing borders, no gradients, no marketing slogan. A serious highly legible operational console with an orderly editorial hierarchy, fewer boxes and more aligned rows. All numbers are fictional. Persistent badge "V2 设计稿 · 示例数据" at upper right.
Left sidebar about 190px: small gold SR monogram, "SixRabbits Stock", section "研究进化中心". Nav: "每日研究" selected, "策略进化", "实验档案", "模拟组合", "研究日报". Bottom text "仅模拟研究" and locked "正式切换须人工批准".

Main header breadcrumb "研究进化 / 每日执行". Title "每日研究台". Small subtitle "按交易日执行，按证据完成". On right date selector "2026-09-18", outlined button "查看日报", small "V2 设计稿 · 示例数据".
Below header a full-width slim running-status rail: amber indicator "研究运行中"; progress "9 / 11 阶段完成"; ID "CYCLE-20260918-001"; right "待处理 1" with small coral dot, and "最后更新 16:46:08".
A minimal 4-column metric strip separated only by vertical lines, labels/values: "数据快照 / DS-0918-01", "可用因子 / 128 / 128", "并行方案 / 1 正式 · 2 挑战者 · 1 影子", "未到期观察 / 12 批". Do not make these gigantic score cards.

Main content split 66% left and 34% right. LEFT dominant panel heading "每日研究流程" with small text "执行状态与验证结论分别记录". Render a beautifully aligned eleven-row process ledger grouped into three subtle labelled sections, with columns "阶段 / 执行状态 / 最新产物". Thin vertical connecting line for numbered steps, compact readable rows. Exact rows:
section "数据与选股"
01 数据更新与质检 / 已完成 / 质量报告 DQ-0918
02 全 A 因子计算 / 已完成 / 因子截面 FS-0918
03 自动选股 / 已完成 / 4 个方案 · 选股清单
04 入选与排除分析 / 已完成 / 规则证据 + 解释
section "验证与模拟"
05 成本化回测 / 已完成 / 复用 2 项 · 新算 1 项
06 独立样本外验证 / 已完成 / 2 项通过 · 1 项执行失败
07 模拟组合更新 / 已完成 / 当日核算 + 次日计划
section "评价与决策"
08 5 / 20 / 60 日评价 / 已完成 / 到期 8 批 · 待到期 12 批
09 新旧方案比较 / 已完成 / 同口径对照 CMP-039
10 保留或淘汰 / 运行中 / 正在生成处置建议
11 生成日报 / 排队中 / 等待处置结果.
Step 06 selected with subtle neutral highlight and a small gold left marker. Its stage execution status is completed, but its result "1 项执行失败" is coral. Step10 amber animated-looking static arc icon; step11 outlined grey icon, not falsely completed.

RIGHT top contextual panel "当前查看 · 独立样本外验证". Small sublabel "阶段已执行，结果并非全部通过". Three compact candidate records:
"EXP-039 / 通过 / 冻结协议 VAL-019"
"EXP-042 / 通过 / 冻结协议 VAL-022"
"EXP-043 / 执行失败 / 缺失验证区间数据".
A clearly visible restrained coral callout "仅阻断 EXP-043 的后续验证" and smaller "正式方案日常研究继续执行". Two outlined buttons "查看缺失证据" and "重试失败任务". Below a simple audit note "重试追加记录，不覆盖原始失败".

RIGHT lower panel "待到期评价", segmented control "5 日 / 20 日 / 60 日" with 60日 selected. One main record "EXP-042" with label "已观察 32 / 60 个交易日", thin progress bar a little over halfway. Large muted dash for "60 日超额收益 —", badge "未到期". Text "不会用未完成窗口生成验证结论". Link "查看所有观察批次 →".

Bottom main-width calm horizontal summary banner "今日日报 · 草稿" with a small document icon, left text "已汇总数据质量、选股原因与模拟组合变化"; right "保留/淘汰阶段完成后归档" and button "预览草稿". Bottom footnote "收盘后生成次日模拟计划 · 未到期不等于失败 · 所有数据仅为界面演示".
Composition must be spacious and professional, with no dense charts on this page. Prioritize exact readable text and hierarchy, especially the difference between completed execution, failed research inputs, and waiting maturity. Render every row and all 11 requested stages without abbreviating away meaning.
```

## 2. 02-strategy-evolution.png

视觉参考：`01-daily-research.png`，仅用于保持品牌、配色、排版与导航风格。

```text
Use case: ui-mockup. Create ONE NEW high fidelity desktop product screen, 2560x1600 landscape 16:10, flat screenshot, sharply legible simplified Chinese. Supplied image is STYLE REFERENCE ONLY: same SixRabbits Stock matte-black and charcoal enterprise research console, champagne-gold primary actions, ivory type, muted teal research improvements, muted coral adverse evidence, thin grey borders, measured 24px padding, 8px corners, excellent aligned tabular typography. No devices, perspective, neon, giant slogan, or decorative backgrounds. This is redesigned Screen 2, "策略进化台", focusing on ONE meaningful strategy comparison instead of cramming daily execution into the page. Persistent upper-right badge "V2 设计稿 · 示例数据". ALL charts and figures are illustrative.

Reuse reference sidebar exactly in form and styling: SixRabbits Stock; 研究进化中心; 每日研究, 策略进化 SELECTED, 实验档案, 模拟组合, 研究日报. Bottom 仅模拟研究 and lock 正式切换须人工批准.
Main header breadcrumb "研究进化 / 方案比较"; title "策略进化台"; modest subtitle "先看证据，再决定是否替换". Right outlined "查看实验档案" and gold "审查晋级 · 1".
Three restrained horizontal summary blocks, not flashy dashboard KPIs: left "当前正式 / 稳健多因子 v18 / EXP-018"; middle gold-selected "对比挑战者 / 低换手优化 v19 / EXP-039 · 待人工审批"; right neutral "影子观察 / 容量约束 v07 / 18日 · 继续观察". Keep cards compact and equal-height.
Below a single contract strip with shield/check icon: "可比性检查通过" and labelled chips "同股票池", "同固定252交易日", "同基准", "同5000万资本", "同费用与执行模型"; right "CMP-039 · 查看协议". This establishes comparable numbers.

Main large 2-column area 56% left and 44% right:
LEFT panel title "费用后表现", subtitle "冻结样本外窗口 · 252 个交易日 · 示意曲线". A clean, detailed line chart with good spacing and grid, gold challenger v19, ivory official v18, dashed grey market benchmark. Y-axis net asset value from 0.96 to 1.12. All three begin at 1.000; end EXACTLY at gold 1.102, ivory 1.084, grey 1.040, with correct vertical positions, occasional natural drawdowns. X-axis Q1 Q2 Q3 Q4. Endpoint labels show those exact values. Chart legend "挑战者 v19", "正式 v18", "基准". Below chart a separate narrow drawdown plot labelled "回撤", no invented performance captions. Lower two short evidence callouts with ample padding: teal "改善：净超额 +1.8pp，回撤收窄 1.5pp"; coral "退步：容量粗估下降 25%". Footnote "窗口、基准和资金规模变更后须重新对照".
RIGHT panel title "完整指标对照", elegant table headers "指标 / 正式 v18 / 挑战者 v19 / 结论", 9 readable rows:
净超额收益 / +4.4% / +6.2% / 改善
最大回撤 / −9.6% / −8.1% / 改善
Sharpe / 1.08 / 1.26 / 改善
信息比率 / 0.62 / 0.78 / 改善
IC / 0.036 / 0.041 / 改善
RankIC / 0.052 / 0.058 / 改善
单边换手 / 28.4% / 21.6% / 改善
容量粗估 / 0.8亿 / 0.6亿 / 退步
方向命中 / 54.2% / 55.0% / 仅辅助.
Footnote "展示样本量与不确定性；不以准确率或单一总分晋级". Use research improvement teal and explicit words, not ambiguous stock colors.

Bottom row three panels:
1 larger "不同市场阶段" containing four readable rows with small colored indicators:
"上涨 · 低波动 / 64 日 / 改善"
"上涨 · 高波动 / 42 日 / 退步"
"下跌 · 低波动 / 68 日 / 改善"
"下跌 · 高波动 / 78 日 / 改善"
small note "阶段按当时可见信息定义 · 3/4 改善，弱项仍须解释".
2 "证据成熟度", two independent status rows:
"EXP-039 前瞻 60/60 日 · 已成熟"
"EXP-042 前瞻 32/60 日 · 未到期"
thin separate progress bars full vs just-over-half; small note "观察完成不自动等于正式晋级".
3 "下一步优化", clear name "参与率约束压力测试"; reason "针对容量退步，保持因子权重不变"; badge "先登记假设，再运行"; button "查看实验计划 →".
Footer "候选可持续迭代 · 正式方案只经人工批准切换 · 示例数据不代表实际收益".
This should be the same exceptionally polished coherent design system as reference but a substantially different information layout. Prioritize readability, orderly spacing, visual sophistication, and clear improvements AND regressions.
```

## 3. 03-experiment-approval.png

视觉参考：`01-daily-research.png`，仅用于保持品牌、配色、排版与导航风格。

```text
Use case: ui-mockup. Create ONE NEW flat high fidelity desktop web product screenshot, 2560x1600 landscape 16:10. Reference image is STYLE REFERENCE ONLY: same SixRabbits Stock V2 Chinese research console. Matte near-black #0c0e11, charcoal #15181d, champagne gold #d5b46a, ivory Chinese sans-serif typography, monospaced evidence IDs, muted teal and coral states, hairline dividers, 8px corners, disciplined padding. No device framing or perspective, no gradients or neon. Persistent badge "V2 设计稿 · 示例数据". This is Screen 3: "实验档案" with an OPEN right-side human promotion review drawer. All example results are fictional. Make Chinese text precise and highly readable, avoid tiny filler paragraphs.

Left sidebar same as reference: SixRabbits Stock / 研究进化中心 / 每日研究 / 策略进化 / 实验档案 SELECTED / 模拟组合 / 研究日报; bottom 仅模拟研究, lock 正式切换须人工批准.
Main area takes left approximately 62% of remaining screen; open right drawer takes 38%, anchored top to bottom with a single fine gold left divider. Underlying archive is undimmed and legible.
Main header breadcrumb "研究进化 / 实验档案". Title "低换手优化 v19". Monospaced ID "EXP-20260615-039"; badge "挑战者 · 待审批"; parent "父方案：稳健多因子 v18". Small tabs "假设与变更" selected, "版本与证据", "完整时间线".

Top archive panel title "本次修改了什么，为什么". Gold modest hypothesis label followed by "降低换手，能否提升费用后超额收益？". Three-row before/after table headers "变更 / 原方案 / 本次":
"调仓间隔 / 每日 / 每5个交易日"
"排名缓冲 / 无 / 5位"
"股票池与因子 / 原始配置 / 保持不变".
Single line rationale "隔离执行规则的影响，避免同时修改多个变量". Small label "验证协议 VAL-019 · 实验前冻结".

Next archive row split two slim panels:
LEFT "可追溯版本" with exact key-value list:
"数据  DS-20260615-01"
"股票池  UNIV-A-0615"
"因子  FACTOR-v12"
"参数  CFG-7a92e4"
"代码  Git a91c72d"
"执行 / 费用  EXEC-v3 / COST-v2"
"环境  ENV-2026.06"
"大模型  模型快照 R3"
"提示词 / 工具  P-v08 / T-v05"
bottom link "原始请求、响应与校验和 →".
RIGHT "完整时间线" with small ordered line/ticks and six events:
"06-15 冻结假设与版本"
"06-16 成本回测完成"
"06-17 独立样本外验证"
"06-18 启动隔离模拟"
"09-17 前瞻观察成熟"
"09-18 提交晋级申请"
footnote "只做模拟研究，无真实交易". Do NOT invent extra live-trading wording.

Next archive thin panel "判断与证据": two rows with icons:
"支持 / 换手下降改善成本 / EVD-039"
"不支持 / 容量同步提升 / EVD-044"
readable small link "查看当时输入与到期结果 →".
Bottom archive panel "成功与失败都保留" with tabs "全部" selected / "失败" / "退役", four-row table:
"EXP-039 / 待人工审批 / 净超额改善，容量退步"
"EXP-036 / 已退役 / 成本后优势消失"
"EXP-033 / 执行失败 / 基础指标缺失"
"EXP-029 / 假设不成立 / 高波动阶段回撤加深".
No rows silently removed.

RIGHT drawer top label "人工审批", title "正式方案切换", X close icon. Strong transition "稳健多因子 v18 → 低换手优化 v19" and experimental ID.
A restrained gold-lock callout "候选不会自动覆盖正式方案". Below a short step strip "核对证据 → 人工批准 → 下个交易日生效".
Section "晋级前核对" with check rows:
"独立验证协议 VAL-019 / 通过"
"前瞻观察 60/60 日 / 已成熟"
"风险与容量约束 / 符合协议"
"当前正式版本 v18 / 仍一致".
Section "必须同时审阅的结果", three compact lines:
teal "净超额收益 +1.8pp"
teal "最大回撤 改善1.5pp"
coral "容量粗估 0.8亿 → 0.6亿".
Note "容量底线0.5亿为本例协议参数，需审阅压力测试".
Section "生效与回退": fields "生效：下一允许交易日", "回退：稳健多因子 v18".
Three EMPTY unchecked square checkboxes with clear labels:
"已审阅独立验证和不利证据"
"已确认成本、容量与风险约束"
"已核对生效时间和回退版本".
Input labelled "输入实验编号" with ghost placeholder "EXP-20260615-039".
Text area labelled "审批意见（必填）", blank.
Small note "确认人、证据包与版本校验将进入审计记录".
Bottom buttons outlined "暂不切换" and visibly DISABLED muted-gold "批准并安排生效". Grey caption "完成核对及必填项后方可批准".
Drawer footer "静态设计稿 · 不会执行切换".
Preserve a very professional, calm scientific product aesthetic, complete failure history and provenance, explicit human authority, and ample breathing room. Never make the disabled approve control look active. One screenshot only.
```
