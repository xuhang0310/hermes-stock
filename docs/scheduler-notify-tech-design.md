# 定时任务与飞书推送 技术方案

> 适用版本：SixRabbits Stock（内部代号 hermes-stock）
> 落地范围：甲方《A股多智能体研究团队方案 v2.1》第六节「每日节律」的自动化运转体系
> 状态：已实现并部署上线（生产服务器 · release `20260710121445`）

---

## 一、背景与目标

系统原有能力是**对话式**多智能体研究台（问股、数字人、持仓、自选、模型配置）。本期补齐甲方方案第六节要求的**自动运转体系**：让系统按每日节律，在固定时间点用「正确的数字人角色」自动生成研究报告，并推送到飞书群，形成「盘前—盘中—盘后—日报」的闭环，同时把这些定时任务做成可视化管理页。

三个交付目标，对应用户确认的两步走 + 可视化：

1. **数据推送工具**：接入飞书自定义机器人 WebHook，双通道（告警群 / 日报群），凭据加密存储、发送留痕。
2. **定时调度**：进程内调度器，按任务用对应数字人无头生成报告并推送。
3. **可视化定时任务**：前端「定时任务」页，任务列表、启停、手动触发、运行历史；「通知通道」设置页。

**安全边界不变**：系统只做研究、监控、提醒、复盘，不连接券商、不自动下单、不构成投资建议。

---

## 二、总体架构

```
                          ┌─────────────────────────────────────────┐
                          │  进程内调度器 rhythm_scheduler（asyncio）│
                          │  每 30s tick → 判定 due → 认领 → 派发    │
                          └───────────────┬─────────────────────────┘
                                          │
                 ┌────────────────────────┼────────────────────────┐
                 ▼                                                  ▼
   ┌───────────────────────────┐                    ┌───────────────────────────┐
   │ 规则类任务（不经 LLM）    │                    │ Agent 类任务              │
   │ health_check：纯模板检查  │                    │ agent_report：            │
   │ （盘中 L0 后续接入）      │                    │ 无头调用 run_agent_events │
   └────────────┬──────────────┘                    │ → 数字人生成报告          │
                │                                    └────────────┬──────────────┘
                │ 异常才推                                        │ 落 agent_reports
                ▼                                                 ▼
        ┌───────────────┐                              ┌──────────────────────┐
        │ 告警群（红卡）│◄─────── feishu.py ──────────►│ 日报群（蓝卡）        │
        │ 零噪音        │  签名 + 重试 + 限流 + 审计   │ 简报/快评/日报        │
        └───────────────┘                              └──────────────────────┘
                          notifications 表（每次发送先 pending 后 sent/failed 留痕）
```

### 三线纪律（甲方硬约束，已在代码层强制）

| 线 | 通道 | 内容 | 是否经 LLM |
| --- | --- | --- | --- |
| 告警线 | `feishu-alert`（告警群） | L0 警报、系统故障、健康检查异常 | 否（模板直出） |
| 日报线 | `feishu-daily`（日报群） | 盘前简报、开盘快报、收盘快评、晚间日报等 | 是（数字人生成） |
| 归档线 | 不推送 | 盘后深研等只落库 `agent_reports` | 是 |

强制点：`agent_report` / `l1_digest` 类任务在 API 层被禁止绑定 `purpose=alert` 的通道（`PUT /schedule/jobs` 返回 400「LLM 生成内容不允许推入告警群」），保证告警群零噪音不被日报污染。

---

## 三、数据模型

新增 3 张表（`Base.metadata.create_all` 自动建表，无 Alembic），复活 2 张原 dead-schema 表。

### 3.1 `notify_channels`（推送通道）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | int PK | |
| channel_key | varchar(80) unique | 通道标识，内置 `feishu-alert` / `feishu-daily` |
| name | varchar(120) | 显示名 |
| channel_type | varchar(40) | 预留（默认 `feishu_webhook`，未来可扩展钉钉/邮件） |
| purpose | varchar(24) | `alert` / `daily`，三线纪律校验依据 |
| webhook_url_cipher | text | **Fernet 密文** |
| secret_cipher | text | **Fernet 密文**（飞书签名密钥） |
| status | varchar(24) | `enabled` / `disabled` |
| remark / last_ok_at / last_error | | 备注、最近成功时间、最近错误 |
| created_at / updated_at | datetime | |

### 3.2 `scheduled_jobs`（任务定义）

结构化时间字段，非 cron 表达式，便于前端可视化编辑。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| job_key | varchar(80) unique | 任务标识（seed 幂等键） |
| name / description | | 中文名、说明 |
| job_kind | varchar(24) | `agent_report` / `health_check` / `rule_scan` / `l1_digest` |
| schedule_type | varchar(24) | `daily` / `weekly` / `monthly_last` / `interval` |
| run_time | varchar(5) | `"08:30"`（daily/weekly/monthly_last 用） |
| weekday_mask | varchar(20) | `"1,2,3,4,5"`（isoweekday，逗号分隔；空=每天） |
| interval_minutes / window_start / window_end | | 盘中区间任务用（如每 30 分、09:30–15:00） |
| trading_day_only | bool | 是否仅交易日执行 |
| digital_human_code | varchar(80) | 数字人角色 code |
| agent_name | varchar(80) | 映射到 `agent_profiles.agent_name`（写 AgentReport 用） |
| prompt_template | text | 支持 `{date}` `{weekday}` `{time}` 占位 |
| push_channel_key | varchar(80) | 推送目标通道；NULL=不推送 |
| enabled / sort_order | | 启停、时间轴排序 |
| last_run_at / last_status / last_error / next_run_at | | 运行状态摘要 |

### 3.3 `job_runs`（运行记录）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| run_id | varchar(120) unique | uuid |
| job_key | varchar(80) index | |
| **fire_key** | varchar(120) | **与 job_key 组合唯一约束 `uq_job_run_fire`，认领锁防重复触发** |
| trigger_type | varchar(24) | `schedule` / `manual` |
| status | varchar(24) | `running` / `success` / `failed` / `skipped` |
| started_at / finished_at / duration_ms | | |
| report_id / notification_id | | 关联 agent_reports / notifications（无外键，对齐既有风格） |
| output_preview / error | text | 结果预览 / 错误 |

### 3.4 复用的既有表

- `notifications`：每次飞书发送先插 `pending`，成功改 `sent`、失败改 `failed`，全程留痕。
- `heartbeats`：调度器每 tick 写 `rhythm_scheduler` 心跳，`status` 接口据此判活。
- `agent_reports`：Agent 报告落库，`agent_name` 走数字人→agent 映射（外键指向 `agent_profiles`）。
- `system_logs`：调度器/飞书模块的运行与异常日志。

---

## 四、后端模块

### 4.1 飞书推送 `backend/app/feishu.py`

- **加密**：`STOCK_WEBHOOK_ENC_KEY` 派生 Fernet 密钥（未配置时回退 `auth_secret`/`db_password` 派生，并告警提示）；webhook/secret 只存密文，API 只回传掩码。
- **签名**：飞书自定义机器人规范 `HMAC-SHA256(key=f"{timestamp}\n{secret}", msg="")` → base64，payload 附 `timestamp`+`sign`。
- **发送 `send_feishu`**：先插 `pending` 审计行 **并 commit 释放数据库连接**，再做外部 HTTP（避免重试窗口长期占用连接池）；`interactive` 卡片，告警红卡/日报蓝卡，正文超 4000 字截断。
- **重试与限流**：仅对网络错误与 5xx 退避重试（1/2/4s），飞书业务错误码（如签名错）不重试；令牌桶每通道 20 条/分钟。
- **SSRF 白名单**：webhook 域名强制 `open.feishu.cn` / `open.larksuite.com` 且 https，防 test 接口被用作内网探测。
- **可观测**：密文解密失败（多因加密密钥变更）与「未配置」区分，暴露 `cipher_invalid`，前端提示「密文解密失败，请重新保存」。
- 语义封装：`send_alert`（告警群，供规则引擎 L0 直推）、`send_daily`（日报群）。

### 4.2 调度器 `backend/app/rhythm_scheduler.py`

仿 `prompt_recommendations.py` 的 `start / stop / _loop` 三件套，FastAPI lifespan 启停，**零新依赖**（不引入 APScheduler/croniter）。

- **时区**：`now_sh()` = `datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)`，与全库 naive datetime 兼容，不依赖服务器系统时区。
- **交易日**：`weekday < 6` 且不在 `STOCK_HOLIDAYS` 清单内。
- **due 判定**：daily / weekly（weekday_mask）/ monthly_last（含「月末逢周末取最后交易日」的正确处理）/ interval（盘中区间）。
- **认领防重**：每分钟 `fire_key = job_key@YYYY-MM-DDTHH:MM`，插 `job_runs` 命中唯一约束即认领成功；重复 tick / 进程重启 / 多 worker 都靠 IntegrityError 天然防重。
- **派发执行**：认领后 `create_task` + `to_thread` 后台执行（Semaphore 限并发 2），不阻塞 tick 循环，长任务不会挤掉后续槽位。
- **health_check**（不经 LLM）：检查 DB 连通、心跳超时、近 12h ERROR 日志、近 24h 失败任务；全正常则静默（告警群零噪音），异常才 `send_alert`。
- **agent_report**（无头调用）：注入数字人 persona（查 `DigitalHumanProfile` 拼 `system_prompt`）→ 消费 `run_agent_events` 生成器收集最终答案（收到 error/异常时显式 `generator.close()`）→ 独立短会话写 `AgentReport`（`agent_name` 经 `DH_TO_AGENT` 映射）→ 按 `push_channel_key` 推送。LLM 消费期间不持有数据库连接。
- **容错**：任务失败只落 `system_logs`，**不推告警群**（守零噪音，由次日健康检查汇总）；启动时把遗留 `running` 记录标 `failed`（进程重启恢复）。
- **数字人 code → agent_name 映射**（两套 code 体系）：
  `stock-pm→pm`、`risk-officer→risk`、`market-sentinel→sentinel`、`research-analyst→researcher`、`intelligence-officer→intelligence`、`quant-analyst→quant`、`macro-strategist→macro`、`short-term-hunter→hunter`。

### 4.3 API `backend/app/rhythm_api.py`

全部走 `Depends(require_permission(...))`，读用 `system:view`、写用 `model:manage`、手动触发用 `data:write`；错误文案中文。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/notify-channels` | 通道列表（webhook 脱敏、secret 只回 has_secret） |
| PUT | `/api/notify-channels/{key}` | 保存（留空=保留原值，域名白名单校验，密文加密） |
| POST | `/api/notify-channels/test` | 测试推送（支持未保存表单先测 / 测已存通道） |
| GET | `/api/schedule/jobs` | 任务列表 |
| PUT | `/api/schedule/jobs/{job_key}` | 编辑（三线纪律校验 + 数字人 code 校验） |
| POST | `/api/schedule/jobs/{job_key}/toggle` | 启停 |
| POST | `/api/schedule/jobs/{job_key}/run` | 手动触发（独立 fire_key，无视交易日/时间窗；运行中返回 409） |
| GET | `/api/schedule/runs` | 运行历史（可按 job_key / status 过滤） |
| GET | `/api/schedule/status` | 调度器心跳判活、是否交易日、今日成功/失败计数 |

### 4.4 安全红线修复

`stock_agent.py` 的 `run_sql` 只读沙箱**原本无表白名单**（`FORBIDDEN_SQL` 只禁写操作/注释/系统库）。已把 `notify_channels`、`scheduled_jobs`、`job_runs` 三张表名加入 `FORBIDDEN_SQL`，配合密文存储双保险，确保 Agent 无法通过 `SELECT` 读出 webhook/secret。

---

## 五、前端

Vue 3 + TS，无路由，`App.vue` 按 `activePage` 切换视图。

- **通知通道设置页** `NotifyChannelSettingsView.vue`（设置→通知通道 Tab，按 `model:manage` 门禁）：左侧编辑表单，右侧通道表（每行内联「编辑 / 测试」）。
- **定时任务页** `RhythmView.vue`（侧边栏「定时任务」，按 `system:view` 门禁）：顶部状态条（调度器判活、交易日、今日成功/失败）、任务表（每行内联「编辑 / 启停 / 执行 / 历史」+ 启用状态圆点）、编辑表单、运行历史；5 秒轮询状态。
- **通用列表组件**：抽出 `list-table` + `cell-actions` 样式类，操作按钮内联到每行「操作」列（替代原先堆在表格下方、带任务名前缀的按钮）；已统一到数据源、模型平台、模型配置、通知通道、定时任务 5 个页面。
- API 与类型集中在 `api.ts` / `types.ts`，沿用 bearer token（localStorage）。

---

## 六、内置节律任务

调度器启动时 `ensure_default_jobs` 幂等预置（只补缺行，不覆盖用户改动）。对应甲方节律表：

| job_key | 时间 | 类型 | 数字人 | 推送 | 默认启用 |
| --- | --- | --- | --- | --- | --- |
| health-check-0815 | 工作日 08:15 | health_check | —（规则） | 异常→告警群 | ✅ 是 |
| premarket-brief-0830 | 工作日 08:30 | agent_report | 情报官 | 日报群 | 否 |
| open-report-0935 | 工作日 09:35 | agent_report | 盘中哨兵 | 日报群 | 否 |
| close-review-1530 | 工作日 15:30 | agent_report | 短线猎手 | 日报群 | 否 |
| postmarket-research-1900 | 工作日 19:00 | agent_report | 个股研究员 | 不推送 | 否 |
| evening-daily-2200 | 每天 22:00 | agent_report | 总调度 PM | 日报群 | 否 |
| weekend-review | 每周六 10:00 | agent_report | 量化分析师 | 日报群 | 否 |
| month-end-review | 月末 16:30 | agent_report | 风控复盘官 | 日报群 | 否 |

> 设计取舍：除健康检查外的 agent_report 任务**默认停用**——一旦启用即到点调 LLM 生成并推送，故留给运维在配好飞书通道与模型、确认效果后逐个启用。盘中 L0/L1（`rule_scan`/`l1_digest`）为后续阶段，依赖上游规则引擎向 `events` 表写入。

---

## 七、配置项

`backend/.env`（`STOCK_*` 优先，回退 `RABBIT_*`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| STOCK_WEBHOOK_ENC_KEY | 空（派生兜底） | 飞书密文加密密钥，**建议显式配置长随机串**，避免与 auth_secret 耦合导致密文失效 |
| STOCK_SCHEDULER_ENABLED | true | 调度器开关（测试环境可关） |
| STOCK_SCHEDULER_TICK_SECONDS | 30 | tick 间隔 |
| STOCK_HOLIDAYS | 空 | 逗号分隔的节假日 `YYYY-MM-DD`，静默盘中/仅交易日任务 |

数据库、LLM、TuShare/Tavily 等沿用既有配置。

---

## 八、部署方案

沿用服务器既有的 **release + 软链** 流程（`/opt/hermes-stock`），原子切换、可回滚。

- 目录：`current`（软链）→ `releases/<时间戳>`；`shared`（logs/data）；`venv`（依赖已装齐）。
- 进程：systemd `hermes-stock.service` = 单 uvicorn 进程 `--host 127.0.0.1 --port 18080`，**单 worker**（保证调度器不重复推送）。
- 反代：nginx listen `18088`，静态 root=`current/frontend/dist`，`/api/`→`18080`。
- 前端：服务器无 node，**本地 `npm run build` 后连 `dist` 一起上传**。
- 流程：SFTP 传代码+dist → **复制旧 release 的 `backend/.env`**（`.env` 每 release 独立，不在 shared）→ 追加新配置 → chown hermes → `ln -sfn` 切 current → `systemctl restart` + `nginx reload` → 验证（health / 新路由 401 / 通道数 / 任务数 / 前端 JS）→ 失败自动回滚。
- **跨环境注意**：webhook 密文与加密盐绑定，部署后需在目标环境用其后端 API 重新保存一次 webhook（否则 `cipher_invalid`）。

---

## 九、验证与测试

- **单元**：加解密往返、飞书签名、卡片截断、`run_sql` 表封堵、调度谓词（daily/weekly/monthly_last×交易日×周末、interval）。
- **集成（真实 MySQL + TestClient）**：通道 CRUD/脱敏/SSRF 负例、三线纪律 400、手动触发 health_check（success）、无头 agent_report 端到端（真实调 qwen3.7-plus 生成晚间日报，AgentReport `agent_name=pm` 正确落库）、409 防重。
- **真实推送**：生产环境重配双通道后，测试推送与晚间日报均 `sent` 成功。
- **回归**：pytest 全绿；前端 `vue-tsc` 类型检查 + 构建通过。

---

## 十、已知边界与后续规划

| 项 | 现状 | 后续 |
| --- | --- | --- |
| 盘中 L0/L1 | 框架已留（interval + rule_scan/l1_digest 类型），未启用 | 依赖规则引擎向 `events` 写入 `level=L0` 记录后接入 |
| 交易日历 | `.env` 手维护节假日清单 | 可改为查 TuShare `trade_cal` 落本地表 |
| 多 Agent 协作 | 单数字人顺序生成 | 可扩展为管道式协作（前者输出拼入后者 prompt） |
| 部署 | 手动脚本 | 可固化为项目内一键部署脚本 |
| 可视化 | 任务表 + 状态条 | 可加节律时间轴视图、倒计时 |

---

*本方案对应代码：`backend/app/{feishu,rhythm_scheduler,rhythm_api}.py`、`models.py`（3 新表）、`settings.py`（4 配置）、`frontend/src/views/{NotifyChannelSettingsView,RhythmView}.vue`。另见 `docs/database.md`、`docs/ask-stock.md`。*
