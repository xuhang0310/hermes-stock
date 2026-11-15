# 平台升级纲领（2026-09）：统一大模型 · 可信性修复 · TuShare 全接口 · 每日研究闭环 · 进化驾驶舱

> 状态：**待办清单（权威版本）**。2026-09-01 基于代码核查（四路并行探查）+ 生产取证（日志/DB/文件系统）编写。
> 每个条目带验收方式；勾选状态以本文件为准。安全红线不变：仅研究/复盘，不接交易通道、不构成投资建议。

---

## 一、现状差距表

### 目标 1：统一大模型（gpt-5.6-sol + reasoning max + service_tier ultrafast + 全量调用留档）

| 现状 | 证据 |
|---|---|
| 激活模型已是 gpt-5.6-sol（`llm_model_configs` id=14, is_active=1, reasoning_effort=max），问股/数字人/定时任务/选股AI 等经 `active_llm_config` 取模型 | 生产 DB 查询（2026-09-01） |
| **service_tier：全仓 0 命中**，模型表/请求/留档均无该字段 | `grep -rn service_tier backend/` |
| 调用留档是 JSONL 哈希链（`ai/ledger_store.py::append_llm_call`），字段仅 model/prompt/response/usage/error/duration_ms——**无 service_tier、无成本、无单价表** | `ledger_store.py:229-251` |
| **agentscope 引擎与 task_extractor 不经 llm_client → 不留档**；流式调用无 usage | `agent_runtime/agentscope_engine.py`、`scheduler/task_extractor.py:268-289` |
| **生产事故：`/opt/hermes-stock/shared/llm_calls/calls.jsonl` 为 root:root 644（8-3 起），服务进程（hermes 用户）写入全部 EACCES，321 次失败，调用留档断档至今** | 生产 `stat` + error.log `[Errno 13]` |
| 静默回退多处：`stock_agent.py:644,688-734` 报错自动切候选模型；`llm_config.py:343-377` 无启用配置时静默回退环境变量（默认 gpt-4.1-mini）；`prompt_recommendations.py:45`、`seed.py:288` 硬编码模型偏好 | 代码核查 |

**差距**：无服务档位透传、无成本核算、留档覆盖不全且生产断档、多处静默降级与"锁定 gpt-5.6-sol"要求直接冲突。

### 目标 2：可信性问题修复（用户列举的 7 项，全部查实）

| 问题 | 现状与证据 |
|---|---|
| 页面说"全 A 股"，策略覆盖只有 12 | 文案 `frontend/src/views/ScreenerView.vue:1421,1436`；候选池=`stock_latest_prices`（同步后 5535 只，接近全市场）；但**策略/回测默认池是硬编码 24 只列表**（`quant/strategy_descriptors.py:20-45`），且加载失败的标的被静默跳过（`quant/backtest_runner.py:628-635`）→ 实际跑十几只且无提示 |
| 回测最大回撤 < -100% | 根因已修（fdc6787，8-12：净值漏计投入本金），但**存量 75 条回测中 36 条 mdd<-100% 原样展示并计入汇总**（生产 DB 查询，全部为 8-11 前旧结果）；残留边界：开仓价缺失→0 价平仓→净值微负（`quant/engines/base.py:361,415`） |
| "数据不足"计入汇总盈利 | 信号/因子回测落库不写 confidence（DB 默认 `insufficient`，`core/models.py:281`）；`calc_metrics` 少于 2 根 K 线返回"数据不足"仍按 `completed` 落库（`backtest_runner.py:400`）；前端历史页汇总不过滤（`BacktestHistoryView.vue:78-97`） |
| 生产混入演示持仓 | `scripts_init_db.py:20` 无条件 `seed_demo`；`POST /api/dev/seed-demo` 生产路由无条件暴露（`main.py:173`）。**生产现存残留：events/agent_tasks/agent_reports/risk_checks/trade_reports 各 1 条 demo-% 行；admin(owner=1) 名下 18 持仓+41 自选含演示数据** |
| 页面统计口径不一致 | 持仓数 3 个口径（`position_query_mixin.py:43-49` 全状态 vs `system_mixin.py:341-353` 去重 open vs `position_account_mixin.py:304-325`）；风险 2 个口径（24h vs 全时段）；**`/api/events`、`/api/reports`、`/api/risk-checks` 用未 for_user 的 repository，owner=None 恒真 → 跨用户全量**（`dashboard_api.py:31,60,69`、`repository.py:22-25`） |
| 选股器 500 | 2026-08-30 实例：`1267 Illegal mix of collations`。**全库 32 张新表是 utf8mb4_0900_ai_ci（DB 默认 collation 即 0900），旧表 unicode_ci，任何跨组 JOIN 都会再炸**（生产 information_schema 查询） |
| Agents/定时任务失败 | `agent_runs` 70 失败：upstream stream interrupted ×5、503 ×3、team 拒绝取消 ×6；`job_runs` 42 失败；今日仍有 `Durable Agent worker failed` |
| 任务看板全 0，调度页大量记录 | 两页读不同表：看板只读 `agent_runs` 且 `board_visible=True`+当前用户（默认 False，仅任务批次子任务置 True，`agent_board_api.py:98-101`、`models.py:770`）；调度页读 `job_runs`（10124 成功）。定时报告仅当任务提取置信度≥0.8 才生成看板任务（`rhythm_scheduler.py:884-941`）→ 看板近乎恒 0 |

### 目标 3：TuShare 全接口接齐 + 三层数据

| 现状 | 证据 |
|---|---|
| 目录是纯代码静态 list，240 条，字段仅 api_name/name_cn/category/scope/points_est/params/default_fields/desc/doc_id/status(stopped/legacy)；**无 DB 持久化，无官方链接/适配状态/真实调用测试/字段版本/数据起止/同步时间/回填进度/缺失率/失败记录/依赖关系** | `market/tushare_catalog.py:9-310` |
| 实际外呼约 20 接口 + 40 语义封装 + 1 通用透传（`tushare_query`，300 行截断、仅文件审计）；**实际在调的 factor_list/factor_value 不在目录白名单** | `stock_tools.py`、`tushare_factor_service.py:72,249` |
| **无 raw/standard/PIT 分层**：仅扁平表（DailyBar/StockDailyBasic/StockLatestPrice）；无三大报表存储（透传即用即弃）；全库无 announce_date/actual_ann_date | `core/models.py`、`stock_tools.py:2512-2524` |
| **退市股被排除**：`_upsert_instruments` 只写 list_date，delist_date 列存在但从不写；无历史 ST/停复牌/指数成分表 | `market_sync.py:143-145,87-89` |
| **无回填任务**：同步仅手动、窗口 ≤60 天（`_SYNC_DAYS_MAX`）；失败只进内存 stats | `market_sync.py:33-34,320` |
| 泄漏风险：财务取最新 end_date 无披露日过滤（`stock_tools.py:3099-3110`）；回测池=当前存续标的（幸存者偏差）；用不复权 daily 且 adj_factor 未落库；daily_basic 仅最新一日快照 | 代码核查 |
| 当前量级：instruments 5550 / 最新价 5535 / daily_basic 11068 行 / **daily_bars 仅 157864 行（历史极薄）** / 因子值 4494 行 | 生产 DB 查询 |

### 目标 4：每日自动研究闭环

现状：`rhythm_scheduler` 有四阶段日程（08:30 盘前/09:35 开盘/15:30 收盘/22:00 日报/19:00 深研+周末月末），结果落 `job_runs`。**无**全市场因子日算→自动选股→入选/淘汰原因→成本化回测→样本外验证→模拟组合更新→5/20/60 日评估→新旧对比→保留/退役的流水线；**无**超额收益/IC/RankIC/IR/换手/容量/风格稳定性评估体系（`quant/` 无相关代码）。

### 目标 5：进化驾驶舱

现状：`agent_board_service` 仅是任务编排（TaskBatch→TeamRun/Delegation）；**无**实验编号/时间线/变更 diff/数据与代码版本/对错判定证据/退役记录/确认切换入口；前端无驾驶舱页面。回测 `data_fingerprint` 仅 MACD/组合有且是参数串非数据哈希。

### 目标 6：每日自动汇报

现状：22:00 日报落库（report_id+model_name，`rhythm_scheduler.py:415-451`）；飞书卡片纯文本+元数据，**无追溯链接**（`feishu.py:644-715`）；内容不含入选/淘汰原因分析、对错判定复盘、新旧方案对比、故障与数据缺口清单。

### 目标 7：TuShare 月度接口检查

现状：**完全不存在**。无目录/文档/字段月度核对任务，无变更检测，无《TuShare 接口月报》产物；业务数据同步与目录检查未分离。

### 目标 8：最终验收

见第四节验收清单，全部依赖 P0-P5 完成 + 20 个交易日观察期。

---

## 二、实施阶段与待办清单

### P0 可信性紧急修复（目标 2 + 目标 1 的留档断档）

- [ ] **P0-1 collation 治理**：生产 32 张 0900 表 `CONVERT TO utf8mb4_unicode_ci`；`ALTER DATABASE` 默认 collation 改 `utf8mb4_unicode_ci`；`_ensure_schema_upgrades` 加启动自愈（发现非 unicode 表自动转换）+ 护栏测试。
  验收：`information_schema` 全表 collation 一致；选股器/看板跨表 JOIN 查询 200；旧 1267 场景回归测试通过。
- [ ] **P0-2 L3 调用留档恢复**：`calls.jsonl` chown 回 hermes（该文件 8-3 起被 root 身份运行污染为 root:root 644，321 次 EACCES）；校验哈希链，必要时把污染段隔离为独立轮转文件重起链；`ledger_store` 加文件权限防御（创建后 664 + 目录检查，失败告警升级）。
  验收：服务进程新调用成功写入（生产 `tail` 见新记录、无新 EACCES）；`verify_calls()` 通过。
- [ ] **P0-3 演示数据清理 + 注入入口禁用**：删除生产 demo-% 行（events/agent_tasks/agent_reports/risk_checks/trade_reports）与 admin 名下演示持仓/自选（逐行核对后删）；`/api/dev/seed-demo` 加环境开关（生产关闭）；`scripts_init_db` 的 seed_demo 加显式 `--seed-demo` 旗标。
  验收：生产 `demo-%` 计数全 0；生产调 seed-demo 返回 404/403；页面不再出现演示持仓。
- [ ] **P0-4 回测可信性**：36 条 mdd<-100% 存量行标记 `engine_version=broken`（或批量重算），默认列表/汇总排除；回撤出口钳制 ≥-1；0 价平仓边界修复；`calc_metrics`"数据不足"落 `insufficient` 状态而非 completed；信号/因子回测补算并落 confidence。
  验收：生产无 mdd<-100% 记录暴露；历史页汇总只统计 sufficient+completed；构造 2 根以下 K 线回测得到"数据不足"且不计盈利。
- [ ] **P0-5 统计口径统一**：列表/看板端点全部补 `for_user`（events/reports/risk-checks）；持仓数/风险数统一为单一聚合服务并在响应注明口径（状态/时间窗）。
  验收：不同页面持仓数/风险数一致（同用户同口径）；跨用户访问仅见本人数据（双账号核查）。
- [ ] **P0-6 任务看板与调度对齐**：看板增加"调度执行"汇总（读 job_runs）或把定时任务 leader run 置 `board_visible`；页面注明两视图口径差异。
  验收：看板非全 0 且与调度页记录可对应；口径说明文案在页面可见。
- [ ] **P0-7 "全 A 股"文案与默认池**：选股页文案改为展示实际候选池数量（取 sync-status）；策略/回测默认池改从已同步候选池派生（或明示"示例池"），数据加载跳过的标的必须回显数量与名单。
  验收：页面文案与实际数一致；跑一次策略，覆盖标的数与候选池一致、跳过有提示。
- [ ] **P0-8 上游失败治理（止血）**：`Upstream response stream was interrupted`/503 类失败接入重试退避并落可查原因（复用 RUN_MAX_ATTEMPTS），失败任务在看板可见。
  验收：连续 3 个交易日无未处理同类失败；失败原因在看板/日志可查。

### P1 统一大模型与调用审计（目标 1）

- [ ] **P1-1 service_tier 透传**：`llm_model_configs` 加 `service_tier` 列（迁移入 `_ensure_schema_upgrades`）；`llm_client._build_payload` 与 agentscope `extra_body` 透传；配置页可编辑。
  验收：配置 `ultrafast` 后，实际请求体携带该参数（生产抓一次出站日志确认）。
- [ ] **P1-2 调用留档升级为 DB 表 `llm_call_logs`**：字段 trace_id/task_kind/model/service_tier/reasoning_effort/duration_ms/tokens/cost/error/status；单价表计算成本；JSONL 哈希链保留双写；**agentscope 引擎与 task_extractor 接入留档**；流式补 usage（finish 段）。
  验收：任意一次问股/研报/定时任务后，`llm_call_logs` 有对应行且字段齐全；成本非空。
- [ ] **P1-3 禁止静默回退**：生成式任务锁定模型解析（无可用配置→显式 500/明确错误，不再回落环境变量）；`stock_agent` failover 改为**显式报错+告警**（保留配置开关以便回滚）；清理硬编码模型偏好。
  验收：停用唯一模型配置时请求显式报错（错误文案中文、含原因）；无自动切换日志。
- [ ] **P1-4 ultrafast 权限验证**：启动/配置保存时发一次最小调用验证 service_tier 可用性，不可用则配置保存失败并提示（不做运行时静默降级）。
  验收：配置无权限档位时保存即报错；生产 gpt-5.6-sol + ultrafast 实测 200。

### P2 TuShare 全接口台账与三层数据（目标 3）

- [ ] **P2-1 接口登记表落库**：新表 `tushare_interface_registry`（api_name/doc_id/category/官方链接/适配状态/最近真实调用时间与结果/字段版本/数据起止/最新同步时间/回填进度/缺失率/失败计数/依赖/状态枚举），240 条目录全量注册 + factor_list/factor_value 补录；替换静态 list 数据源。
  验收：`GET /api/tushare/interfaces` 返回全量接口与状态；与官方目录逐条比对无遗漏。
- [ ] **P2-2 真实调用测试任务**：定时任务对每个接口发最小参数调用，结果写登记表；状态机：可用/暂无数据/目录隐藏/文档移除/运行失败。
  验收：全接口至少一次真实调用记录；状态分布报表可查。
- [ ] **P2-3 历史回填**：可获取接口全量历史首次回填（daily/adj_factor/daily_basic/三大报表含披露时间/ST/停复牌/指数成分/公告），分批+限速+断点续传，回填进度入登记表。
  验收：核心接口（daily/财务/adj_factor）覆盖起始日期达官方最早日期；进度页显示缺失率 <0.5%。
- [ ] **P2-4 三层建模**：raw 层（原样落库，含拉取时间戳）→ standard 层（清洗/对齐交易日历/复权）→ PIT 层（按 announce_date/actual_ann_date 时点可见性）；财务数据全部带披露时间存储。
  验收：任给 (指标, 股票, 历史日期)，PIT 查询只返回当时已披露数据；抽 3 只股票人工比对公告日期。
- [ ] **P2-5 防偏差数据集**：退市股保留（delist_date 写入+退市标记）、历史 ST 标记、停复牌区间、历史指数成分快照；回测标的池切换为"时点存续池"；回测行情切换复权价。
  验收：回测池含已退市标的（历史区间）；用 PIT 池重跑旧策略结果与旧结果有差异报告。
- [ ] **P2-6 缺失与失败台账**：缺失率统计任务 + 失败重试记录表 + 数据质量看板端点。
  验收：看板显示每接口缺失率/最近失败；构造缺失日后自动补拉成功。

### P3 每日自动研究闭环（目标 4）

- [ ] **P3-1 全市场因子日算**：收盘后批量计算（本地因子+TuShare 因子），结果落库带数据指纹。
- [ ] **P3-2 自动选股流水线**：因子/策略选股 → 入选/淘汰原因分析（LLM 结构化）→ 成本化回测（统一成本模型）→ 独立样本外验证（时间切分，禁止参数窥探）。
- [ ] **P3-3 模拟组合与评估**：研究用模拟组合自动更新（仅 `paper_*` 表，无交易通道）；5/20/60 日滚动评估：超额收益、最大回撤、夏普、IR、IC/RankIC、换手、容量、分行情阶段稳定性。
- [ ] **P3-4 新旧对比与保留/退役**：新方案进"挑战者"，与"正式"并行评估；达到阈值才提交人工确认切换；退役记录原因。
  P3 整体验收：连续 5 个交易日全自动跑通无人工介入；每日产物（选股名单+原因+回测+样本外+组合变动）在平台可查。

### P4 进化驾驶舱（目标 5）

- [ ] **P4-1 实验模型**：`experiments` 表（唯一实验编号/父实验/正式|挑战者|影子状态/时间线/变更 diff：数据版本+股票池+因子+参数+代码版本+LLM 版本）。
- [ ] **P4-2 判定证据链**：每次判定对错落证据（引用回测 run_id/实验编号/数据区间）；退役记录含原因。
- [ ] **P4-3 驾驶舱前端**：当前正式/挑战者/影子一览、完整实验历史（含失败）、改进/退步对比、下一步优化计划及理由；**自动迭代不得自动覆盖正式策略，切换需确认入口（二次确认+权限）**。
  验收：任一实验可点击查看全链路证据；无确认不可能变更正式策略（构造测试证明）。

### P5 每日汇报升级 + TuShare 月度检查（目标 6、7）

- [ ] **P5-1 日报内容升级**：覆盖"做了什么/为什么/选了哪些股及原因/哪些判定成熟/对错及原因/新旧对比/下一步优化/当前故障与数据缺口与风险"；每个结论带追溯链接（→数据/回测/实验）。
- [ ] **P5-2 月度接口检查任务**：每月第一个周日 09:00 核对上一自然月官方目录/文档/字段变更（新增/字段变更/隐藏/移除/恢复）；对变化接口最小真实调用+兼容性测试；业务数据保持原同步频率不变。
- [ ] **P5-3 《TuShare 接口月报》**：当日 20:00 生成：覆盖率/变化明细/调用结果/缺失与失败/回填进度/受影响因子策略/已完成修复/待办；平台可查看/下载/追溯。
  验收：飞书日报每条结论可点击跳转；月报定时产物连续 2 期正常生成。

### P6 观察期与最终验收（目标 8）

- [ ] **P6-1 20 个交易日观察**：全链路运行，未处理严重故障为 0；期间所有失败有处理记录。
- [ ] **P6-2 最终验收清单**（逐项过）：见第四节。

---

## 三、预计完成时间（单人顺序推进估算，不含等待用户确认的停顿）

| 阶段 | 工作量 | 预计完成 |
|---|---|---|
| P0 可信性紧急修复 | ~5 个工作日 | 2026-09-08 |
| P1 统一大模型与调用审计 | ~4 个工作日 | 2026-09-12 |
| P2 TuShare 台账+回填+三层 | ~15 个工作日（回填受 TuShare 积分/限速制约，可能拉长） | 2026-10-02 |
| P3 每日研究闭环 | ~8 个工作日 | 2026-10-14 |
| P4 进化驾驶舱 | ~5 个工作日 | 2026-10-21 |
| P5 日报升级+月度检查 | ~5 个工作日 | 2026-10-28 |
| P6 20 个交易日观察+终验 | 自然时间约 4 周 | 2026-11-25 |

总计约 12 周。样本外验证与观察期是自然时间，不可压缩。

---

## 四、最终验收清单（对应目标 8）

1. [ ] 所有生成式任务的调用留档显示实际使用 gpt-5.6-sol + reasoning max + ultrafast；抽 10 条覆盖问股/研报/复盘/选股/日报。
2. [ ] TuShare 全接口在册，含真实运行状态；核心接口覆盖率与缺失率达标记线。
3. [ ] 全 A 股普通股正常标的覆盖率 ≥99.5%（以官方上市数为分母，排除长期停牌/退市按规则处理）。
4. [ ] 生产无演示数据（`demo-%` 全 0，无演示持仓/风控/报告）。
5. [ ] 回测可复现（同参数同数据指纹结果一致）且无 < -100% 回撤。
6. [ ] 各页面统计口径一致（双账号核查）。
7. [ ] 每日研究/选股/迭代/汇报全自动运行（连续记录可查）。
8. [ ] 连续 20 个交易日无未处理严重故障。
9. [ ] 用户可查看全部成功与失败迭代（驾驶舱演示走查）。
