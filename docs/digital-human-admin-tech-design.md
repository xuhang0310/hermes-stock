# 数字人管理页（画像 + 能力开关可视化） 技术方案

> 适用版本：SixRabbits Stock（内部代号 hermes-stock）
> 落地范围：把数字人画像与能力开关（写持仓等）从"只能改数据库"升级为后台可视化管理页
> 状态：方案设计，待评审实现

---

## 一、背景与目标

写持仓功能有"三重门"：**数字人画像开关**（`digital_human_profiles.can_write_positions`）× research 模式 × 用户 `data:write` 权限。实际使用中用户在「问股助手」里录持仓被拒，排查半天才发现是第一道门——该画像的开关没开，而这个开关**目前只能直接改数据库**，运营者完全看不见、摸不着。

进一步盘点发现缺口比一个开关更大：

| 能力 | 后端 API | 前端 UI |
| --- | --- | --- |
| 数字人画像 CRUD（名称/提示词/快捷问题/状态/排序） | ✅ `POST/PUT /api/digital-humans`（`digital_human:manage` 门禁） | ❌ **没有任何页面调用**（`api.saveDigitalHuman` 定义了但零引用） |
| 写持仓开关 `can_write_positions` | ❌ 不在 `DigitalHumanIn` 出入参里 | ❌ 无 |
| 每个数字人"有哪些工具" | 散在 `COMMON_TOOL_NAMES` / `ROLE_TOOL_NAMES` / 技能 registry | ❌ 无处可看，用户只能问数字人自己 |
| 飞书机器人绑定 | ✅ | ✅ 数字人中心「飞书机器人」Tab（已有） |

三个交付目标：

1. **数字人管理页**：设置页新增「数字人管理」Tab，画像全字段可视化编辑（补上缺失的 UI）。
2. **能力开关可视化**：写持仓开关做成列表内即时 toggle，一眼看见谁能写、一键启停，全程审计。
3. **能力矩阵**：每个数字人可展开查看"它到底有哪些工具"（公共工具/角色专属/写工具三类，来自技能 registry 实时计算），终结"agent 说自己没工具、用户以为是权限问题"的排查黑洞。

**安全边界不变**：三重门逻辑一行不动——UI 开关只是把第一道门做成可视化；聊天时仍要求用户具备 `data:write`、仍走"预览→确认→落库"人在环流程；飞书机器人链路保持不注入写工具（无登录身份，过不了第三道门）。

---

## 二、总体设计

```
设置页（SettingsView）
 └─ Tab「数字人管理」（门禁 digital_human:manage，admin/super_admin 可见）
     ├─ 顶部统计条：数字人总数 / 启用数 / 可写持仓数 / 飞书绑定数
     ├─ 列表（复用 list-table + cell-actions 通用样式，与模型/数据源/通道五页统一）
     │    列：名称 | code | 状态 | 目标角色 | 写持仓[toggle] | 飞书绑定 | 工具数 | 操作(编辑/能力/启停)
     │    · 写持仓 toggle 点击即 PUT，行内即时生效，无需进编辑表单
     ├─ 编辑表单（选中行展开）：name / code / avatar_tone / target_roles /
     │    description / system_prompt(多行) / quick_prompts(逐条) / sort_order / status
     │    / can_write_positions(开关+三重门说明文案)
     └─ 能力抽屉（点「能力」展开，只读）：
          ├─ 公共工具（COMMON：快照/搜索/最新价…）
          ├─ 角色专属工具（ROLE_TOOL_NAMES[code]）
          ├─ 写持仓工具（仅开关开启时列出 update_my_positions 等 3 个）
          └─ 每个工具显示中文名 + 一句话说明（复用 display_name + schema description）
```

### 关键设计取舍

1. **放在设置页而不是数字人中心**：数字人中心是"用"（聊天），设置页是"管"（配置），与模型平台/数据源/通知通道的信息架构一致；飞书绑定已在数字人中心有面板，列表里只放只读状态徽章并跳转。
2. **写持仓 toggle 行内直改**：这是本方案的原始诉求，必须一步到位；其它字段改动才进编辑表单。
3. **能力矩阵只读**：本期不做"按数字人勾选工具"（那是技能系统 Phase 4 的 `enabled_skills`），只做"看得见"；矩阵数据从 registry 实时算，代码即文档、永不漂移。
4. **PUT 是全量模型**：现有 `DigitalHumanIn` 全字段必填，前端编辑必须回填完整数据再提交，避免误清 `system_prompt`；toggle 场景由前端带上该行现有全字段 + 翻转后的开关值。

---

## 三、后端改动（小，~1 小时）

全部在 `backend/app/ai/ai_api.py`，无新表、无迁移（`can_write_positions` 列已存在）。

| # | 改动 | 说明 |
| --- | --- | --- |
| 1 | `DigitalHumanIn` 增 `can_write_positions: bool = False` | 默认 False 与建表默认一致 |
| 2 | `_digital_human_out` 增 `can_write_positions`、`feishu_bot_enabled`（只读徽章用） | 列表/详情都带出 |
| 3 | `update_digital_human` / `create_digital_human` 持久化该字段 | `model_dump` 自然带入，确认无遗漏即可 |
| 4 | **审计**：写持仓开关变化时落 `system_logs`（module=digital_human，记录操作人/数字人/旧值→新值） | 对齐 `run_sql` 审计精神：权限开关的每次变化可追溯 |
| 5 | 新增 `GET /api/digital-humans/{id}/capabilities`（`agent:use` 可读） | 返回 `{common_tools[], role_tools[], write_tools[], can_write_positions}`，每项 `{name, display_name, description}`；数据来自 `digital_human_tool_names(code)` + `skill_registry` schema + `WRITE_TOOL_NAMES`，实时计算不落库 |

> 注意：GET `/api/digital-humans` 是 `agent:use` 门禁（聊天页也用它拉列表），新增的 `can_write_positions` 字段对 viewer 可见无妨（只是布尔，不含敏感信息）；**修改**仍走 `digital_human:manage`。

---

## 四、前端改动（主体，~半天）

| # | 文件 | 内容 |
| --- | --- | --- |
| 1 | `views/DigitalHumanAdminView.vue`（新增） | 统计条 + 列表 + 编辑表单 + 能力抽屉，样式复用 `list-table` / `cell-actions` / `page-section` |
| 2 | `views/SettingsView.vue` | 新增「数字人管理」Tab，按 `digital_human:manage` 权限显隐 |
| 3 | `api.ts` | `digitalHumanCapabilities(id)`；`saveDigitalHuman` 已有直接用 |
| 4 | `types.ts` | `DigitalHuman` 增 `can_write_positions` / `feishu_bot_enabled`；新增 `DigitalHumanCapabilities` |

交互细节：

- **toggle 即时保存**：点击开关 → 乐观翻转 → PUT（回填全字段）→ 失败回滚并提示；成功后行内出现"✅ 已开启写持仓（仍需用户具备 data:write 且经预览确认）"的短暂提示。
- **危险提示**：给风控官/哨兵这类"设计上只读"的角色开写持仓时，弹一次确认框说明设计取舍（可开，但要知情）。
- **能力抽屉**懒加载：点开才请求 capabilities，避免列表页 N+1。

---

## 五、安全红线

1. **三重门不动**：UI 只管理第一道门；`ai_api.agent_chat` 的 `can_write_positions = profile开关 × research × 用户data:write` 判定原样保留，viewer/researcher 即使面对开了开关的数字人也拿不到写工具。
2. **改动可追溯**：开关每次变化落 `system_logs`（操作人、数字人、旧值→新值、时间），出问题能回答"谁在什么时候把风控官的写权限打开了"。
3. **写入仍人在环**：预览→复述确认→`confirmed=true` 才落库、可撤销批次，这套流程与 UI 开关无关、不受影响。
4. **飞书链路不放开**：飞书机器人无登录身份，`run_agent_events` 调用继续不传写开关；如未来要放开，必须先做"飞书用户↔系统账号"绑定方案，单独评审。
5. **`code` 字段编辑保护**：内置 8 角色 + 问股助手的 `code` 是工具分配/调度映射的主键（`ROLE_TOOL_NAMES` / `DH_TO_AGENT`），编辑表单对已存在记录的 `code` 置灰只读，防止改坏映射。

---

## 六、分阶段落地

| 阶段 | 内容 | 工作量 | 交付 |
| --- | --- | --- | --- |
| **P1** | 后端字段透出+审计 + 设置页「数字人管理」Tab（统计条/列表/编辑表单/**写持仓 toggle**） | ~半天 | 开关可视化、画像全字段后台可改，本方案原始诉求闭环 |
| **P2** | `capabilities` API + 能力抽屉（工具矩阵可视化） | ~半天 | "这个数字人有什么工具"一目了然 |
| **P3** | 对接技能系统 Phase 4：`enabled_skills` 按数字人勾选技能（复用本页，抽屉从只读变可编辑） | 依赖技能系统 Phase 4 | 能力从"看得见"到"配得动" |

P1/P2 可以一次做完（合计约一天）；P3 等技能系统 Phase 4 排期。

---

## 七、测试与验证

- 后端：PUT 带/不带 `can_write_positions` 的持久化与默认值；审计日志落库断言；capabilities 返回与 `digital_human_tool_names` 一致性（逐角色对比）；`digital_human:manage` 负例（researcher 改开关 → 403）。
- 前端：`vue-tsc` + 构建；toggle 乐观更新失败回滚；编辑表单全字段回填（重点：保存后 `system_prompt`/`quick_prompts` 不丢）。
- 端到端（生产）：关掉问股助手开关 → 聊天请求写持仓被拒；打开 → 预览/确认流程可用；system_logs 有两条审计记录。

---

*相关代码：`backend/app/ai/ai_api.py`（DigitalHumanIn/out、digital-humans 端点）、`backend/app/market/digital_human_tools.py`（COMMON/ROLE_TOOL_NAMES/WRITE_TOOL_NAMES）、`backend/app/skills/registry.py`（工具 schema 来源）、`frontend/src/views/SettingsView.vue`。相关方案：[skill-system-tech-design.md](skill-system-tech-design.md)（Phase 4 enabled_skills 与本页 P3 对接）。*
