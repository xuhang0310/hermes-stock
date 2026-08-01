# Agent 任务执行引擎 — 测试报告

> 日期：2026-08-01
> 版本：feat/agent-task-execution-engine 分支
> 测试范围：定时任务产出 → 任务提取 → 批次创建 → Worker 执行 → 状态回写

---

## 1. 功能概述

定时任务执行后，若产出需要其他数字人协同的动作，系统自动创建看板任务并调度执行。支持两种提取模式：

| 模式 | 触发时机 | 特点 |
|------|---------|------|
| `structured_tool` | Agent 执行中直接调用工具 | leader 暂停等待子任务完成，强一致性 |
| `legacy_report` | Agent 完成后从报告提取 | fire-and-forget，leader 不等结果 |

## 2. 单元测试（42 项全绿）

### 2.1 提取器校验逻辑 — `TaskExtractorValidateTests`（8 项）

| 用例 | 验证内容 | 结果 |
|------|---------|------|
| `test_validate_empty_tasks` | 无任务时返回 `has_tasks=false` | ✅ |
| `test_validate_has_tasks_normal` | 正常任务通过校验，字段正确传递 | ✅ |
| `test_validate_rejects_unknown_agent_code` | 不存在的 agent_code 被拒绝并产生 warning | ✅ |
| `test_validate_rejects_duplicate_action_key` | 批次内重复 action_key 被去重并产生 warning | ✅ |
| `test_validate_rejects_missing_required_fields` | 缺少必填字段（action_key/title）的任务被跳过 | ✅ |
| `test_validate_truncates_at_max_tasks` | 超过 max_tasks 上限时截断并产生 warning | ✅ |
| `test_validate_non_boolean_has_tasks` | `has_tasks` 非布尔值时安全降级为 false | ✅ |
| `test_validate_tasks_not_list` | `tasks` 字段非数组时安全降级 | ✅ |

### 2.2 提取器转换 — `TaskExtractorToBatchTests`（2 项）

| 用例 | 验证内容 | 结果 |
|------|---------|------|
| `test_to_task_batch_converts_plan` | 提取结果正确转为 `TaskBatchSpec`，字段一一对应 | ✅ |
| `test_to_task_batch_empty_tasks` | 空任务列表生成空 batch | ✅ |

### 2.3 提取器完整流程 — `TaskExtractorExtractTests`（4 项）

| 用例 | 验证内容 | 结果 |
|------|---------|------|
| `test_extract_empty_content` | 空报告内容提前返回，不调用 LLM | ✅ |
| `test_extract_no_agents_available` | 无可委派 Agent 时提前返回 | ✅ |
| `test_extract_with_mocked_llm` | Mock LLM 返回正常 JSON → 提取成功 → `_save_extraction_plan` 被调用 | ✅ |
| `test_extract_llm_failure` | LLM 调用失败 → 返回空结果，包含 warning | ✅ |

### 2.4 Worker 串行约束 — `SerialConstraintTests`（5 项）

测试 `repository.next_queued_run()` 的 NOT EXISTS 子查询：

| 用例 | 验证内容 | 结果 |
|------|---------|------|
| `test_no_running_returns_queued` | 无 running 任务时正常返回 queued | ✅ |
| `test_running_blocks_same_agent` | 同 assignee 有 running → queued 被阻塞（即使优先级更高） | ✅ |
| `test_running_does_not_block_different_agent` | Agent A running 不阻塞 Agent B 的 queued | ✅ |
| `test_no_assignee_not_blocked` | 无 assignee_profile_id 的任务不受串行约束 | ✅ |
| `test_waiting_children_also_blocks` | `waiting_children` 状态同样触发串行约束 | ✅ |

**测试要点**：使用两个独立 `AiChatSession` 隔离 session 级约束，精确测试 agent 级串行。

### 2.5 Worker 孤儿恢复 — `OrphanRecoveryTests`（5 项）

测试 `_recover_orphan_board_tasks` 扫描逻辑：

| 用例 | 验证内容 | 结果 |
|------|---------|------|
| `test_recovers_old_queued_board_tasks` | queued 超 5 分钟的 board_visible 任务被回收 | ✅ |
| `test_skips_recent_queued_tasks` | 刚入队（< 5分钟）的任务不被误回收 | ✅ |
| `test_skips_non_board_tasks` | `board_visible=false` 的任务不被回收 | ✅ |
| `test_skips_cancel_requested_tasks` | 已请求取消的任务不被回收 | ✅ |
| `test_empty_when_no_orphans` | 无孤儿时返回空列表 | ✅ |

### 2.6 工具 Schema 注册 — `OrchestrationToolSchemaTests`（3 项）

| 用例 | 验证内容 | 结果 |
|------|---------|------|
| `test_schema_registered` | `create_agent_task_batch` 在 `ORCHESTRATION_TOOL_NAMES` 和 `TOOL_DISPLAY_NAMES` 中 | ✅ |
| `test_schema_structure` | schema 包含 objective/tasks/execution_mode 等必填字段，tasks 内字段齐全 | ✅ |
| `test_prompt_rule_mentions_tool` | ORCHESTRATION_PROMPT 中明确提及必须调用此工具 | ✅ |

### 2.7 调度器委派配置 — `ChannelRunsDelegationConfigTests`（2 项）

| 用例 | 验证内容 | 结果 |
|------|---------|------|
| `test_build_request_includes_delegation_config` | `delegation_config` 被正确透传到 `request_payload` | ✅ |
| `test_build_request_without_delegation_config` | 不传时默认 `None` | ✅ |

### 2.8 看板服务原子操作 — `AgentBoardServiceTests`（13 项，已有）

覆盖 `create_task_batch`、`retry_failed_task`、`cancel_task`、`update_task_priority`、`get_task_queue_for_agent` 五个方法。

---

## 3. 代码审查修复（4 处）

| # | 严重度 | 问题 | 文件 | 修复 |
|---|--------|------|------|------|
| 1 | 🔴 | `request_json` f-string 拼接，title 含引号 → 无效 JSON | `agent_board_service.py:184` | 改用 `json.dumps()` |
| 2 | 🔴 | `session_id=0` 写入 NOT NULL FK（无 id=0 的 session） | `agent_board_service.py:171` | 继承 `leader.session_id` |
| 3 | 🔴 | `leader_run_id or ""` 空字符串写入 NOT NULL FK | `agent_board_service.py:126-127` | 显式 `raise ValueError` |
| 4 | 🔴 | `legacy_report` 路径必崩：leader 已 completed，`create_task_batch` 要求 running | `agent_board_service.py` + `rhythm_scheduler.py` | `_from_legacy_report=True` 跳过状态校验 |

---

## 4. 未覆盖的测试场景

以下场景需要真实 MySQL + 真实 LLM，未在自动化测试中覆盖：

| 场景 | 原因 | 风险 |
|------|------|------|
| Agent 真实调用 `create_agent_task_batch` 工具 | 需要完整 AgentScope 引擎运行环境 | 中：工具参数传递、事件流是否正确 |
| 真实 LLM 提取任务（`legacy_report`） | 需要 LLM API 调用，结果不确定 | 中：LLM 返回格式不稳定的降级能力 |
| Worker 实际领取并执行子任务 | 需要真实 Redis + Worker 进程 | 低：逻辑在单测中已覆盖 |
| 定时任务完整循环 | 需要等待真实定时触发 | 低：各模块已单独验证 |
| 并发竞争（两个 worker 同时 claim） | 需要多进程测试环境 | 低：MySQL FOR UPDATE 保证原子性 |

**建议**：部署后通过创建测试定时任务（`delegation_mode=execute_and_wait`）在生产环境验证端到端流程。

---

## 5. 结论

| 维度 | 状态 |
|------|------|
| 核心逻辑正确性 | ✅ 42 项单测全绿 |
| 代码安全性 | ✅ 代码审查修复 4 处生产级 bug |
| 端到端集成 | ⚠️ 未自动化，需部署后验证 |
| 生产就绪 | ⚠️ 代码就绪，建议在测试环境跑一次完整循环后再上生产 |
