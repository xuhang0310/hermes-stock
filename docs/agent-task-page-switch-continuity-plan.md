# 数字人 Agent 跨页面持续执行方案

> 适用系统：SixRabbits Stock（`hermes-stock`）  
> 目标：用户在数字人中心提交任务后，即使切换页面、刷新页面或 SSE 连接中断，任务仍在后端继续执行；返回数字人中心后恢复进度或展示最终结果。  
> 约束：不引入 Redis、消息队列或新的基础设施，复用现有 FastAPI + MySQL + SQLAlchemy + SSE + 独立 background 服务。

## 1. 问题与根因

当前数字人任务的生命周期与一次 HTTP/SSE 请求绑定：

```text
DigitalHumanCenter.vue
  -> POST /api/agent/chat
  -> StreamingResponse.event_stream()
  -> 请求内部创建 produce_events 线程
  -> SSE 持续向浏览器推送
```

页面通过 `v-if / v-else-if` 切换。离开数字人中心时，组件状态被销毁；一旦浏览器侧连接关闭，后端 `event_stream()` 检测到 `request.is_disconnected()` 后会执行 `cancel_event.set()`，终止 Agent。断开时还会跳过助手消息保存，所以返回页面后既没有运行状态，也没有完整结果。

当前设计混合了三个本应独立的职责：

1. **任务执行**：调用模型、工具和审批流程。
2. **任务状态**：记录排队、运行、完成、失败和取消。
3. **实时展示**：通过 SSE 把过程显示给当前页面。

只要实时展示连接消失，任务执行就被停止，这是问题的根因。

## 2. 目标行为

```text
10:00:00  用户提交任务，获得 run_id
10:00:05  用户切换到持仓页面，SSE 订阅断开
10:00:10  background 服务继续调用行情工具
10:00:25  background 服务完成任务并保存答案
10:01:00  用户返回数字人中心
10:01:01  页面恢复任务记录，直接展示已完成结果
```

必须满足：

- 切换页面、组件卸载、SSE 断开不取消任务。
- 只有用户点击“停止任务”才设置取消标记。
- 返回页面能够恢复 `running / waiting_approval / completed / failed / cancelled` 状态。
- 已完成答案必须进入现有 `ai_chat_messages`，继续作为会话历史使用。
- API A/B 副本均能读取任务状态，不依赖某个 API 进程的内存。
- 不高频逐 Token 写 MySQL，避免流式输出造成数据库压力。

## 3. 总体架构

复用现有部署：

```text
                    MySQL
                      ^
                      |
浏览器 -> Nginx -> API A/B ---- 创建任务、查询状态、SSE订阅
                      |
                      v
              background service ---- 领取并执行 Agent 任务
```

职责调整：

| 组件 | 调整后职责 |
| --- | --- |
| API A/B | 鉴权、创建任务、查询任务、将 MySQL 中的快照转换成 SSE；不执行长任务 |
| background 服务 | 轮询并领取 MySQL 中的任务，调用 `run_agent_events()`，持续写状态快照和最终结果 |
| MySQL | 任务事实来源、断线恢复、最终结果和少量关键过程事件 |
| SSE | 只负责观察任务；断开不会控制任务生命周期 |
| 前端 | 保存 `run_id`，进入页面时重新查询并订阅，不把组件内存当作任务事实来源 |

该方案不要求 Redis。实时性由 API 每 300～500ms 查询一次任务 `version` 实现；系统当前规模下足够使用。

## 4. 数据模型

不建议直接复用现有 `background_tasks`。该表用于知识库重建，缺少用户、会话、结果、取消、心跳等字段。新增专用表能避免污染已有任务逻辑。

### 4.1 `agent_runs`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `run_id` | varchar(64) PK | 前端持有的任务ID，UUID/ULID |
| `user_id` | int index | 所属用户 |
| `session_id` | int index | 对应 `ai_chat_sessions` |
| `user_message_id` | int | 已写入的用户消息，重试时避免重复插入 |
| `assistant_message_id` | int nullable | 完成后对应的助手消息 |
| `digital_human_id` | int nullable | 数字人 |
| `model_config_id` | int nullable | 模型配置快照引用 |
| `chat_mode` | varchar(24) | `research` 等 |
| `status` | varchar(24) index | `queued/running/waiting_approval/completed/failed/cancelled/interrupted` |
| `stage` | varchar(40) | 当前阶段，如 `model/tool/approval/finalizing` |
| `payload_json` | longtext | 问题、附件、skills、审批策略等不可变输入 |
| `reasoning_snapshot` | longtext | 已聚合的思考过程，可按产品策略省略或裁剪 |
| `answer_snapshot` | longtext | 当前已生成答案 |
| `steps_json` | longtext | 模型选择、工具开始/完成、文件等关键步骤 |
| `version` | bigint | 每次可见快照更新递增，供断线续传 |
| `cancel_requested` | bool | 用户显式取消标记 |
| `event_count` | int | 关键事件数 |
| `duration_ms` | int nullable | 总耗时 |
| `error` | text nullable | 失败原因 |
| `worker_id` | varchar(80) nullable | 执行进程标识，便于排障 |
| `heartbeat_at` | datetime nullable | Worker 心跳 |
| `created_at` | datetime | 创建时间 |
| `started_at` | datetime nullable | 开始时间 |
| `finished_at` | datetime nullable | 完成时间 |
| `updated_at` | datetime | 快照更新时间 |

推荐索引：

```text
(user_id, status, updated_at)
(session_id, created_at)
(status, created_at)
```

### 4.2 `agent_run_events`（仅关键事件）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | bigint PK | 自增ID |
| `run_id` | varchar(64) index | 所属任务 |
| `seq` | int | 任务内事件序号 |
| `event_type` | varchar(40) | `model/action/observation/artifact/approval/error/done` |
| `payload_json` | longtext | 事件内容 |
| `created_at` | datetime | 时间 |

唯一约束：`(run_id, seq)`。

不要把每个 `content_delta` 和 `reasoning_delta` 插成一行。Token 流只更新 `agent_runs.answer_snapshot/reasoning_snapshot`，关键步骤才写 `agent_run_events`。

## 5. MySQL 写入节流

后台执行时仍可逐 Token 在进程内累积，但按以下任一条件批量更新快照：

- 距离上次写入达到 500ms；
- 新增文本达到 1KB；
- 遇到工具调用、审批、错误或完成事件；
- 任务退出前强制刷盘。

每次刷盘：

```sql
UPDATE agent_runs
SET answer_snapshot = ?,
    reasoning_snapshot = ?,
    steps_json = ?,
    version = version + 1,
    heartbeat_at = NOW(),
    updated_at = NOW()
WHERE run_id = ?;
```

这样一次一分钟的任务通常只产生几十到一百余次 UPDATE，而不是数千次 INSERT。

## 6. 后端执行流程

### 6.1 创建任务

新增：

```http
POST /api/agent/runs
```

处理步骤放在短事务内：

1. 完成现有权限、数字人、模型、附件和审批策略校验。
2. 创建或读取 `AiChatSession`。
3. 立即写入用户 `AiChatMessage`。
4. 创建 `AgentRun(status='queued')`，保存不可变输入。
5. 提交事务并立即返回。

响应：

```json
{
  "run_id": "01J...",
  "session_id": 128,
  "status": "queued",
  "version": 1
}
```

### 6.2 background 服务领取任务

现有部署已有唯一的 `hermes-stock-background.service`，在其 lifespan 中启动一个 `agent_run_worker_loop()`：

```text
每500ms查询一个queued任务
-> 条件更新 queued -> running
-> 更新成功表示领取成功
-> asyncio.to_thread(run_agent_job, run_id)
-> 受全局Semaphore限制并发
```

即使未来误启动两个 background 实例，也用条件更新避免重复领取：

```sql
UPDATE agent_runs
SET status='running', worker_id=?, started_at=NOW()
WHERE run_id=? AND status='queued';
```

只有 `rowcount == 1` 的进程可以执行。

### 6.3 执行任务

从现有 `/agent/chat` 中抽出与 HTTP 请求无关的部分，形成：

```python
def run_persisted_agent(run_id: str) -> None:
    # 用 SessionLocal 读取输入
    # 调用现有 run_agent_events
    # 聚合 delta，节流更新 agent_runs
    # 关键步骤写 agent_run_events
    # 完成后写 AiChatMessage + completed
```

必须保证 `run_agent_events()` 不引用 Starlette `Request` 或当前请求的数据库 Session。现有实现已经在后台线程内为部分工具使用独立 `SessionLocal`，可以沿用。

### 6.4 取消

新增：

```http
POST /api/agent/runs/{run_id}/cancel
```

接口只更新：

```text
cancel_requested = true
```

Worker 在以下位置检查：

- 每轮模型调用前；
- 每个工具调用前后；
- 等待审批期间；
- 每次快照刷盘时。

发现取消后才设置进程内 `cancel_event`，最终状态写为 `cancelled`。

**SSE 断开绝不能再调用 `cancel_event.set()`。**

## 7. 状态查询与SSE恢复

新增接口：

```text
GET /api/agent/runs?status=active
GET /api/agent/runs/{run_id}
GET /api/agent/runs/{run_id}/stream?after_version=12
POST /api/agent/runs/{run_id}/cancel
```

### 7.1 状态详情

`GET /runs/{run_id}` 返回完整当前快照，必须按 `user_id` 做所有权校验。

### 7.2 SSE订阅

SSE 不再承载任务执行，只做 MySQL 观察器：

```text
读取agent_runs
-> version > after_version 时发送snapshot事件
-> 查询并发送未见过的关键事件
-> completed/failed/cancelled时发送done并关闭
-> 无变化时发送ping
```

建议轮询间隔：前台可见时 300～500ms；没有变化时逐步退避到 1s。每次查询必须使用短生命周期 `SessionLocal`，不能在整个 SSE 生命周期中长期持有数据库连接。

恢复订阅：

```http
GET /api/agent/runs/01J.../stream?after_version=12
```

服务端可以直接先返回最新完整快照，不要求重放所有 Token；关键工具和审批事件通过 `agent_run_events` 补齐。

## 8. 前端改造

### 8.1 提交任务

`DigitalHumanCenter.send()` 从直接消费 `/agent/chat` 改为：

```text
POST /agent/runs
-> 得到run_id/session_id
-> 在消息区创建running占位消息
-> subscribeRun(run_id)
```

### 8.2 页面返回恢复

组件 `onMounted`：

1. 加载会话。
2. 查询当前用户 `queued/running/waiting_approval` 任务。
3. 找到当前会话对应任务后，用任务快照重建占位消息。
4. 从返回的 `version` 建立 SSE 订阅。
5. 如果任务已经完成，加载现有会话消息即可，不重复订阅。

可将最近的 `run_id` 写入 `sessionStorage` 作为快速定位提示，但 MySQL 才是事实来源；即使浏览器存储丢失，也能通过活动任务接口恢复。

### 8.3 页面切换

页面卸载时允许关闭当前 SSE 订阅以节省连接，但不得调用取消接口。返回页面重新订阅即可。

```typescript
onUnmounted(() => {
  streamController?.abort() // 只停止观察
  // 禁止调用 /cancel
})
```

### 8.4 全局状态提示

在 `App.vue` 或侧边栏每 3～5 秒查询一次活动任务数量，显示：

```text
数字人中心  ● 1个任务运行中
```

轮询只查计数和状态，不加载大文本快照。

## 9. 审批流程

现有 `agent_tool_approvals` 已持久化，可继续使用：

1. Worker 创建审批记录并将 `agent_runs.status` 改为 `waiting_approval`。
2. 用户即使在其他页面，任务保持等待，不被取消。
3. 返回数字人中心后，任务详情返回待审批信息。
4. 用户批准后，现有审批接口更新数据库。
5. Worker轮询到审批结果后继续执行，并恢复 `running`。

等待审批是唯一合理的“任务暂停”场景之一，UI必须明确显示“等待你的确认”，不能显示为普通运行中。

## 10. 进程重启策略

本方案首先保证页面切换和浏览器断线不中断。进程崩溃恢复要谨慎处理，因为 Agent 可能已经执行文件写入或持仓修改，自动从头重跑可能重复产生副作用。

第一阶段建议：

- background 服务启动时，将 `running` 且心跳超时的任务标记为 `interrupted`。
- 不自动重跑有副作用的 Agent 任务。
- 前端展示“服务重启导致任务中断，请重试”。
- 用户点击重试时创建新 `run_id`，保留旧任务审计记录。

后续只有在工具全部具备幂等键后，再支持自动恢复或重跑。

## 11. 兼容与迁移

- 保留现有 `POST /api/agent/chat` 一段时间，供问股等尚未迁移的页面使用。
- 先仅迁移 `DigitalHumanCenter.vue` 到 `/agent/runs`。
- 新旧路径最终都复用抽出的输入准备和结果保存函数，避免出现两套业务规则。
- `AiChatSession/AiChatMessage` 继续作为对话事实来源；`AgentRun` 只负责一次执行过程。
- 完成任务时，在一个短事务中同时创建助手消息并把任务标记为 `completed`，避免“显示完成但历史中没有答案”。

## 12. 实施步骤

### Phase 1：执行解耦

1. 新增 `AgentRun`、`AgentRunEvent` 模型和建表逻辑。
2. 抽取现有 `/agent/chat` 的请求准备逻辑。
3. 实现 `/agent/runs` 创建、查询、取消接口。
4. 在独立 background 服务中实现任务领取和执行循环。
5. 完成答案继续落入 `ai_chat_messages`。

### Phase 2：断线恢复

1. 实现基于 MySQL 快照的 SSE 订阅接口。
2. 对文本增量实施 500ms/1KB 节流。
3. `DigitalHumanCenter.vue` 改用创建任务 + 订阅任务。
4. 页面挂载时恢复活动任务。

### Phase 3：体验完善

1. 侧边栏增加运行任务徽标。
2. 增加等待审批、失败、取消、中断状态展示。
3. 增加显式“停止任务”和“重新执行”按钮。
4. 清理超过保留期的过程事件，只长期保留最终消息和任务摘要。

## 13. 测试与验收

### 必测场景

| 场景 | 预期 |
| --- | --- |
| 提交后切换到持仓页2分钟 | 后台继续执行，返回后看到最新进度或结果 |
| 切换页面期间任务完成 | 返回后立即展示完整答案 |
| 浏览器刷新 | 登录恢复后能够找到活动任务并重新订阅 |
| SSE主动断开 | Worker不取消，重新订阅后继续展示 |
| 用户点击停止 | Worker在安全检查点停止，状态为cancelled |
| 等待工具审批时切页 | 状态保持waiting_approval，返回后仍可审批 |
| API A创建任务、API B订阅 | 状态和结果一致 |
| background进程异常退出 | 任务最终标为interrupted，不静默丢失 |
| 同一用户连续提交 | 按产品规则排队或拒绝，不重复执行 |
| 完成事务中断 | 不出现completed但无助手消息的状态 |

### 验收标准

- 页面切换100次不导致任务取消。
- 返回页面后1秒内恢复当前任务状态。
- 正常任务最终答案与现有同步SSE路径一致。
- 没有活动前端订阅时任务仍能完成。
- MySQL写入频率受节流控制，不随Token数量线性增长。
- 用户只能读取和取消自己的任务。

## 14. 风险与控制

| 风险 | 控制措施 |
| --- | --- |
| MySQL快照写入过多 | 500ms/1KB节流，只持久化关键事件 |
| SSE轮询占用连接池 | 每轮使用短Session，空闲退避，限制单用户订阅数 |
| background重复领取 | `queued -> running`条件更新并检查rowcount |
| 任务无限运行 | 最大时长、最大Agent步数、工具超时 |
| 自动重跑重复副作用 | 第一阶段不自动恢复，标记interrupted后人工重试 |
| 大字段无限增长 | reasoning裁剪、steps数量上限、过程事件定期清理 |
| 多标签页重复订阅 | 允许多观察者，但任务只有一个Worker；取消操作需明确确认 |

## 15. 不采用的方案

### 仅使用 `v-show` 或 `KeepAlive`

只能解决普通组件切换，无法解决刷新、标签页关闭和网络断开；任务仍依赖浏览器连接，不作为正式方案。

### SSE断线后让前端重新提交问题

会重复模型费用和工具副作用，也无法判断原任务是否仍在运行。

### 每个Token写一条MySQL事件

实现简单但写放大明显，表增长快。采用“文本快照节流 + 关键事件表”更适合当前架构。

## 16. 结论

基于现有架构，合理修复不是让页面组件一直存活，而是把一次 Agent 执行升级为 MySQL 可追踪的后台任务：

```text
API创建任务
-> 独立background服务持续执行
-> MySQL保存节流快照和最终答案
-> SSE随时订阅/断开/恢复
-> 只有显式取消才停止任务
```

该方案无需 Redis，能够直接复用项目已有的独立 background 服务、`BackgroundTask` 思路、`SessionLocal`、审批表和 SSE 前端能力，并为后续多实例部署保留一致性。
