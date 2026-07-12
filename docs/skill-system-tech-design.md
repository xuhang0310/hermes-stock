# 技能（Skill）系统 技术方案

> 适用版本：SixRabbits Stock（内部代号 hermes-stock）
> 落地范围：把现有硬编码的工具注册/分发链，重构为可插拔、可自动发现、可热加载的技能系统
> 状态：方案设计，待评审实现

---

## 一、背景与目标

当前系统的智能体工具以"散装常量 + 多处 if/elif 分发"组织，新增一个工具要跨 3 个文件改 5+ 处代码，且角色-工具映射、技能提示词全部硬编码在源码里，无法在运行时增删。这套形态在前 16 个 stock 工具、19 个数字人工具阶段尚可承受，但要让数字人能力可被"安装/卸载/按需启用"，就必须把工具与提示词收敛为自描述的、可发现的独立模块——即"技能"。

四个交付目标：

1. **自描述技能文件**：每项技能 = 一个 Python 文件，包含 schema、执行器、角色绑定、提示词指令，零外部注解。
2. **自动发现注册**：进程启动时扫描 `skills/` 目录，自动 import 并注册全部技能；新增技能只需丢文件，不改任何分发链。
3. **统一工具分发**：消灭 `stock_tools.py` / `digital_human_tools.py` / `stock_agent.py` 三处 if/elif，收敛到一个注册中心。
4. **可安装可热加载**：技能可从数据库记录（pip 包名 / git URL）安装，可在运行时 reload 而不重启进程。

**安全边界不变**：系统仍是研究/监控/复盘台，不连券商、不下单、不给投资建议。`run_sql` 只读沙箱、`FORBIDDEN_SQL` 正则、`agent_audit.log` 留痕三条硬约束在重构后必须原样保留。

---

## 二、现状分析（重构起点）

工具系统的现状可用一张表概括：

| 维度 | stock 工具 | 数字人工具 | 持仓代理 | SQL 基础工具 |
| --- | --- | --- | --- | --- |
| schema 定义 | `STOCK_TOOL_SCHEMAS` 原始 dict | `DIGITAL_HUMAN_TOOL_SCHEMAS` 经 `_tool()` | `POSITION_AGENT_TOOLS` 原始 dict | `TOOLS` 原始 dict |
| 执行分发 | `execute_stock_tool` if/elif | `execute_digital_human_tool` if/elif | `_execute_position_tool` if/elif | 内联于 `run_agent_events` |
| 角色筛选 | 无 | `ROLE_TOOL_NAMES` + `profile_code` | 无 | 无 |
| 提示词 | `STOCK_STRATEGIES` | `ROLE_SKILLS` | 内嵌系统提示 | 无 |
| 代码位置 | `market/stock_tools.py` | `market/digital_human_tools.py` | `agents/position_agent.py` | `agents/stock_agent.py` |

核心痛点（按严重程度排序）：

1. **三处分发链重复**：`execute_stock_tool`（[stock_tools.py:446](../backend/app/market/stock_tools.py#L446)）、`execute_digital_human_tool`（[digital_human_tools.py:219](../backend/app/market/digital_human_tools.py#L219)）、`run_agent_events` 内的白名单分发（[stock_agent.py:452](../backend/app/agents/stock_agent.py#L452)）逻辑重叠，新增工具名要同时改三处。
2. **角色-工具映射硬编码**：`ROLE_TOOL_NAMES` / `ROLE_SKILLS`（[digital_human_tools.py:25-149](../backend/app/market/digital_human_tools.py#L25)）写死在源码，`DigitalHumanProfile.code` 是唯一连接点，数据库里改不动。
3. **元数据散落**：工具的 schema、display_name、observation_preview、prompt_instructions 四份元数据分布在不同常量与函数里，无单一来源。
4. **无运行时增删**：要新增一个数字人能力，必须改源码发版重启，无法按客户/按角色在线启用。

> 注：`agents/position_agent.py` 是单用途代理，工具集固定且不参与角色筛选，本期**不纳入**技能化范围，保留现状。同理 `scheduler/rhythm_scheduler.py` 的 `DH_TO_AGENT` 映射（[rhythm_scheduler.py:44](../backend/app/scheduler/rhythm_scheduler.py#L44)）只做角色→AgentProfile 路由，与工具系统正交，不动。

---

## 三、总体架构

```
        ┌─────────────────────────────────────────────────────────────┐
        │  backend/app/skills/                                        │
        │                                                            │
        │   market_data.py   risk_monitor.py   news_digest.py  ...    │
        │   ┌──────────┐    ┌──────────────┐   ┌────────────┐         │
        │   │ SKILL =  │    │ SKILL =      │   │ SKILL =    │  每个文件一个技能 │
        │   │ {name,   │    │ {name, roles,│   │ {name, ... │  自描述、可独立增删 │
        │   │  tools,  │    │  tools, ...} │   │  ...}      │         │
        │   │  executors,│   └──────────────┘   └────────────┘         │
        │   │  roles,  │                                              │
        │   │  prompt} │                                              │
        │   └──────────┘                                              │
        └────────────┬───────────────────────────────────────────────┘
                     │ pkgutil.iter_modules 自动 import
                     ▼
        ┌─────────────────────────────────────────────────────────────┐
        │  SkillRegistry（单例，进程内）                                │
        │  discover() -> _skills / _tool_to_skill / _tool_executors  │
        │  get_tools_for(chat_mode, role_code) -> [schema, ...]      │
        │  execute(tool_name, args) -> dict                          │
        │  get_prompt_instructions(role_code) -> [str, ...]          │
        └────────────┬───────────────────────────────────────────────┘
                     │ 替代三处 if/elif
                     ▼
        ┌─────────────────────────────────────────────────────────────┐
        │  run_agent_events（stock_agent.py）                          │
        │  旧：tools = STOCK_TOOL_SCHEMAS if stock_mode else [拼接]    │
        │      observation = 三层 if/elif 分发                          │
        │  新：tools = skill_registry.get_tools_for(chat_mode, role)   │
        │      observation = skill_registry.execute(name, args)       │
        └─────────────────────────────────────────────────────────────┘
```

### 三条纪律（重构硬约束）

| 线 | 现有位置 | 重构后 | 验证点 |
| --- | --- | --- | --- |
| `run_sql` 只读沙箱 | `stock_agent.py::run_sql` | 作为一个**内置技能** `skills/_builtin_sql.py`，`validate_sql` + `FORBIDDEN_SQL` + `agent_audit.log` 原样保留 | 抽离后仍走 `validate_sql`，仍写 audit log，仍单语句 SELECT |
| 角色-工具绑定 | `ROLE_TOOL_NAMES` 硬编码 | 迁到技能文件的 `roles` 字段；`DigitalHumanProfile.enabled_skills`（新增 JSON 字段）做运行时覆盖 | 默认行为与现有完全一致 |
| 系统提示词注入 | `digital_human_skill_prompt` / `stock_system_prompt_with_skills` | 由 `skill_registry.get_prompt_instructions(role)` 汇总后注入 | 数字人各角色的 skills/output 段落逐字不变 |

---

## 四、核心概念：技能文件结构

每项技能是一个 Python 模块，模块级导出一个 `SKILL` 字典。这是唯一被注册中心识别的契约。

```python
# backend/app/skills/market_scanner.py

SKILL = {
    "name": "market_scanner",          # 全局唯一，下划线命名
    "display_name": "市场扫描",          # 中文展示名
    "description": "扫描全市场热点板块和异动个股",
    "version": "1.0.0",
    "chat_modes": ["research", "stock"],   # 哪些对话模式可见
    "roles": ["stock-pm", "short-term-hunter"],  # 哪些角色可用（空=所有角色）
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "scan_hot_sectors",
                "description": "扫描当前热门板块及其领涨股",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "top_n": {"type": "integer", "description": "返回前 N 个板块", "default": 5}
                    },
                    "required": [],
                },
            },
        },
    ],
    "executors": {
        # tool_name -> callable(args_dict) -> dict
        "scan_hot_sectors": _scan_hot_sectors,   # 函数定义在文件内或从别处 import
    },
    "prompt_instructions": [
        "用户问市场热点时，优先调用 scan_hot_sectors。",
        "扫描结果要结合个股技术面综合判断，不直接给买卖建议。",
    ],
}

def _scan_hot_sectors(args):
    top_n = _int_arg(args.get("top_n"), 5, 50)
    # ... 调 TuShare 板块接口 ...
    return {"sectors": [...]}
```

### 4.1 设计约定

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 全局唯一键，下划线命名；与文件名一致便于查找 |
| `display_name` | 是 | 中文展示名，给前端技能列表用 |
| `tools` | 是 | OpenAI function-calling 格式 schema 列表，可空列表（纯提示词技能） |
| `executors` | `tools` 非空时必填 | `tool_name -> callable`；纯提示词技能可省略 |
| `chat_modes` | 是 | `stock` / `research` 子集；决定哪些模式加载 |
| `roles` | 否 | 角色码列表（与 `ROLE_SKILLS` 的 key 对齐）；空列表 = 所有角色可见 |
| `prompt_instructions` | 否 | 注入系统提示词的行为指令列表 |
| `version` | 否 | 技能版本，用于升级审计 |
| `builtin` | 否 | `True` 表示系统内置不可卸载（`run_sql` 等安全关键技能） |

### 4.2 与现有数据的迁移映射

| 现有硬编码 | 迁移到 | 技能文件 |
| --- | --- | --- |
| `STOCK_TOOL_SCHEMAS` 中行情类工具 | `tools` | `skills/market_data.py` |
| `STOCK_TOOL_SCHEMAS` 中技术分析类 | `tools` | `skills/technical_analysis.py` |
| `STOCK_TOOL_SCHEMAS` 中新闻/资讯类 | `tools` | `skills/news_intel.py` |
| `STOCK_STRATEGIES` | `prompt_instructions` | 并入对应技能 |
| `ROLE_TOOL_NAMES` 各角色条目 | 拆分到各技能的 `roles` 字段 | — |
| `ROLE_SKILLS` 各角色 skills/output | `prompt_instructions` + 角色专属技能文件 | — |
| `run_sql` / `get_schema` + 沙箱 | `builtin=True` 的 `_builtin_sql.py` | 安全红线原样保留 |

---

## 五、技能注册中心

`backend/app/skills/registry.py`，进程内单例，负责发现、查询、执行、热加载。

```python
import importlib
import pkgutil
from pathlib import Path
from typing import Callable, Dict, List

_BUILTIN_DIR = "_builtin"  # 内置技能子目录名，不可卸载


class SkillRegistry:
    """技能注册中心 — 启动自动发现，运行时统一分发。"""

    def __init__(self) -> None:
        self._skills: Dict[str, dict] = {}            # name -> SKILL
        self._tool_to_skill: Dict[str, str] = {}      # tool_name -> skill_name
        self._tool_executors: Dict[str, Callable] = {}  # tool_name -> callable

    # ---- 发现与加载 ----
    def discover(self, package: str = "backend.app.skills") -> None:
        """扫描 skills 包，自动 import 并注册全部技能模块。"""
        pkg = importlib.import_module(package)
        pkg_path = Path(pkg.__file__).parent
        self._skills.clear()
        self._tool_to_skill.clear()
        self._tool_executors.clear()

        for module_info in pkgutil.iter_modules([str(pkg_path)]):
            if module_info.name.startswith("_"):
                continue  # 跳过 __init__、私有模块
            self._load_module(f"{package}.{module_info.name}")

        # 内置技能子包（_builtin/）
        builtin_pkg = f"{package}.{_BUILTIN_DIR}"
        try:
            builtin = importlib.import_module(builtin_pkg)
            for module_info in pkgutil.iter_modules([str(Path(builtin.__file__).parent)]):
                self._load_module(f"{builtin_pkg}.{module_info.name}")
        except ModuleNotFoundError:
            pass

    def _load_module(self, module_path: str) -> None:
        try:
            mod = importlib.import_module(module_path)
        except Exception as exc:  # 单个技能加载失败不拖垮全局
            # 实际走 applog 记录
            print(f"[skill] load failed: {module_path}: {exc}")
            return
        skill = getattr(mod, "SKILL", None)
        if not isinstance(skill, dict) or "name" not in skill:
            return
        self.register(skill)

    def register(self, skill: dict) -> None:
        name = skill["name"]
        if name in self._skills:
            raise ValueError(f"duplicate skill: {name}")
        self._skills[name] = skill
        for tool in skill.get("tools", []):
            tname = tool["function"]["name"]
            self._tool_to_skill[tname] = name
        for tname, fn in skill.get("executors", {}).items():
            self._tool_executors[tname] = fn

    # ---- 查询 ----
    def get_tools_for(self, chat_mode: str, role_code: str, enabled: set = None) -> List[dict]:
        """按对话模式 + 角色 + 启用集合返回可用工具 schema。"""
        tools = []
        for name, skill in self._skills.items():
            if enabled is not None and name not in enabled:
                continue
            if chat_mode not in skill.get("chat_modes", []):
                continue
            roles = skill.get("roles", [])
            if roles and role_code not in roles:
                continue
            tools.extend(skill.get("tools", []))
        return tools

    def get_prompt_instructions(self, role_code: str, enabled: set = None) -> List[str]:
        instructions = []
        for name, skill in self._skills.items():
            if enabled is not None and name not in enabled:
                continue
            roles = skill.get("roles", [])
            if roles and role_code not in roles:
                continue
            instructions.extend(skill.get("prompt_instructions", []))
        return instructions

    def list_skills(self) -> List[dict]:
        """前端技能列表/管理页用。"""
        return [
            {
                "name": s["name"],
                "display_name": s.get("display_name", s["name"]),
                "description": s.get("description", ""),
                "version": s.get("version", ""),
                "roles": s.get("roles", []),
                "chat_modes": s.get("chat_modes", []),
                "builtin": s.get("builtin", False),
                "tools": [t["function"]["name"] for t in s.get("tools", [])],
            }
            for s in self._skills.values()
        ]

    # ---- 执行 ----
    def execute(self, tool_name: str, args: dict) -> dict:
        fn = self._tool_executors.get(tool_name)
        if fn is None:
            return {"error": f"Unknown tool: {tool_name}"}
        try:
            return fn(args or {})
        except Exception as exc:
            return {"error": f"tool {tool_name} failed: {exc}"}

    # ---- 热加载 ----
    def reload(self, package: str = "backend.app.skills") -> None:
        """运行时重新发现并注册；已改的技能文件会被 importlib 重新载入。"""
        pkg = importlib.import_module(package)
        importlib.reload(pkg)
        self.discover(package)


skill_registry = SkillRegistry()  # 全局单例
```

### 5.1 生命周期接入

在 `main.py::create_app(init_db=True)` 的 lifespan 里，schema 初始化之后、路由挂载之前调用一次：

```python
from backend.app.skills.registry import skill_registry

@asynccontextmanager
async def lifespan(app):
    skill_registry.discover()    # 启动自动发现
    # ... 现有调度器启停 ...
    yield
```

---

## 六、工具加载与分发流程（改造后）

### 6.1 `run_agent_events` 改造点

| 位置 | 现状 | 改造后 |
| --- | --- | --- |
| [stock_agent.py:371-373](../backend/app/agents/stock_agent.py#L371) 工具组装 | `tools = STOCK_TOOL_SCHEMAS if stock_mode else [*TOOLS, *digital_human_tool_schemas(...)]` | `tools = skill_registry.get_tools_for(chat_mode, role_code, enabled)` |
| [stock_agent.py:374-378](../backend/app/agents/stock_agent.py#L374) 系统提示词 | 分支调 `stock_system_prompt_with_skills` / `_system_prompt` | 两处函数内部改为从 `skill_registry.get_prompt_instructions` 取指令 |
| [stock_agent.py:445-510](../backend/app/agents/stock_agent.py#L445) 工具分发 | 三层 if/elif（stock / digital_human / base） | `observation = skill_registry.execute(name, args)` 一行 |

> `enabled` 集合来自 `DigitalHumanProfile.enabled_skills`（见第七节），未配置时为 `None` 表示全启用，保证默认行为与现状一致。

### 6.2 改造前后对比（新增一个工具 `scan_hot_sectors`）

```
改造前：改 5 处
  1. stock_tools.py  STOCK_TOOL_SCHEMAS          加 schema
  2. stock_tools.py  execute_stock_tool           加 elif 分支
  3. stock_tools.py  stock_tool_display_name      加映射
  4. stock_tools.py  stock_observation_preview    加分支
  5. stock_agent.py  run_agent_events 白名单       加工具名
  （还要确认 ROLE_TOOL_NAMES 里哪个角色要用）

改造后：改 1 处
  新建 skills/market_scanner.py  写一个 SKILL 字典
  重启 / 调 /api/skills/reload   即生效
```

---

## 七、数据模型（Phase 4，可选）

新增 1 张表，复用 `DigitalHumanProfile` 加 1 个 JSON 字段。`Base.metadata.create_all` 自动建表，无 Alembic。

### 7.1 `skill_packages`（可选：运行时安装来源）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | int PK | |
| name | varchar(80) unique | 技能名，与 SKILL["name"] 对齐 |
| source_type | varchar(20) | `local` / `pip` / `git` |
| source_ref | varchar(255) | pip 包名或 git URL，local 留空 |
| version | varchar(40) | 已安装版本 |
| enabled | bool | 是否启用（软开关） |
| installed_at | datetime | |
| meta_json | text | 技能元数据快照（display_name/tools 等） |

### 7.2 `DigitalHumanProfile` 扩展

| 新字段 | 类型 | 说明 |
| --- | --- | --- |
| `enabled_skills` | Text(JSON) | 角色启用的技能名列表；`null` = 启用全部默认技能 |

`enabled` 优先级：`DigitalHumanProfile.enabled_skills` > 技能文件 `roles` 默认 > 全启用。

---

## 八、API 设计

挂到 `ai/ai_api.py`（AI 配置类端点的归属目录），全部走 `Depends(require_permission(...))`。

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/skills` | `viewer` | 列出全部已加载技能（`skill_registry.list_skills()`） |
| POST | `/api/skills/reload` | `admin` | 热加载：`skill_registry.reload()`，返回新技能列表 |
| GET | `/api/skills/{name}/tools` | `viewer` | 单个技能的工具 schema，调试用 |
| PUT | `/api/digital-humans/{id}/skills` | `admin` | 配置角色 `enabled_skills`，写 `DigitalHumanProfile` |
| POST | `/api/skills/install` | `super_admin` | Phase 4：按 `source_type` 安装新技能包并 reload |

`install` 与 `reload` 是进程级副作用，需在 `require_permission` 之外加操作审计（参考 `run_sql` 的 audit log 模式），落一条 `agent_audit.log` 或独立表。

---

## 九、目录结构

```
backend/app/
  skills/                      # 新增包
    __init__.py                # 导出 skill_registry、调用 discover
    registry.py                # SkillRegistry
    _builtin/                  # 内置安全关键技能，不可卸载
      __init__.py
      _sql_sandbox.py          # run_sql + get_schema + validate_sql + FORBIDDEN_SQL
    market_data.py             # 行情类工具
    technical_analysis.py      # 技术分析类工具
    news_intel.py              # 新闻/资讯类工具
    role_stock_pm.py           # stock-pm 角色专属提示词/工具
    role_risk_officer.py       # risk-officer 角色专属
    ...
  agents/
    stock_agent.py             # 工具组装与分发改为调用 registry
  market/
    stock_tools.py             # Phase 3 后逐步清空，最终可移除
    digital_human_tools.py     # 同上
  ai/
    ai_api.py                  # 新增 /api/skills/* 端点
```

---

## 十、分阶段落地计划

每个阶段独立可发版，互不阻塞。

| 阶段 | 内容 | 改动范围 | 验证 |
| --- | --- | --- | --- |
| **Phase 1** | 建 `skills/registry.py` + `SkillRegistry`，在 lifespan 调 `discover()`；把现有 16 个 stock 工具按类别拆成 3-4 个技能文件 | 纯新增 `skills/` 包，不动现有分发链 | `skill_registry.list_skills()` 能列出全部工具；现有 chat 行为完全不变（registry 只注册不接管） |
| **Phase 2** | 改 `run_agent_events` 工具组装与分发切到 registry；`stock_tools.py` 的分发函数改为转发到 registry | [stock_agent.py:371-510](../backend/app/agents/stock_agent.py#L371) | 跑全套 `pytest`；手动问股、数字人对话回归一致；`run_sql` 沙箱 + audit log 不变 |
| **Phase 3** | 数字人工具与 `ROLE_TOOL_NAMES` / `ROLE_SKILLS` 迁移到技能文件；`digital_human_tools.py` 逐步清空 | [digital_human_tools.py](../backend/app/market/digital_human_tools.py) 全文 | 各角色可用工具集逐字不变；调度器无头报告产出不变 |
| **Phase 4** | 加 `skill_packages` 表 + `DigitalHumanProfile.enabled_skills`；实现 `/api/skills/*` 端点；支持 pip/git 安装与 reload | models + ai_api + 前端技能管理页 | 安装一个示例技能包后 reload，新工具在对话中可用 |

---

## 十一、安全红线与约束

重构不得突破以下任一约束，每条都在对应阶段有验证项：

1. **`run_sql` 只读沙箱**：抽成 `_builtin/_sql_sandbox.py` 技能后，`validate_sql`（单语句 / SELECT-only / 禁注释 / 禁系统库 / 自动 LIMIT）与 `FORBIDDEN_SQL` 正则、`backend/agent_audit.log` 每调用追加，三条原样保留。该技能 `builtin=True`，`/api/skills` 列表可展示但 `install`/卸载接口对其禁用。
2. **research-only 边界**：技能的 `tools` 不得引入任何下单、连接券商、自动委托能力；新增技能在 PR review 时显式确认无出金/下单调用。
3. **`run_agent_events` 事件白名单**：现有 `tool_start/tool_done/action/observation` 事件流契约不变，技能工具执行后仍产出同样的 observation dict，前端 SSE 解析无需改动。
4. **加载失败隔离**：单个技能 import 失败不得拖垮进程，`_load_module` 已 try/except 并落 applog；`discover` 后 `list_skills()` 缺失的技能要能在日志里定位。
5. **热加载副作用**：`/api/skills/reload` 与 `install` 是进程级操作，需 `admin`/`super_admin` 权限 + 审计日志；reload 期间正在进行的对话不受影响（registry 替换的是查询入口，已生成的 tools 列表是该次请求的快照）。

---

## 十二、测试策略

复用现有测试约定（`tests/`，需可达 MySQL，否则跳过）。

| 测试 | 文件 | 覆盖点 |
| --- | --- | --- |
| `test_skill_registry_discover` | `tests/test_skill_registry.py` | discover 后全部内置技能注册成功；重复 name 报错；坏技能文件不影响其他 |
| `test_skill_registry_filter` | 同上 | `get_tools_for(chat_mode, role)` 返回的工具集与改造前 `digital_human_tool_schemas` 逐角色一致 |
| `test_run_sql_sandbox_intact` | `tests/test_backend_api.py` | 抽离后 `run_sql` 仍拒写语句、仍写 audit log、仍单语句 |
| `test_agent_chat_regression` | `tests/test_backend_api.py` | stock / research 两种模式问股回归，事件序列与改造前一致 |
| `test_skill_reload` | `tests/test_skill_registry.py` | reload 后新增/修改的技能可见；进行中的对话不受影响 |

---

## 十三、风险与取舍

1. **抽象成本**：技能系统引入一层注册中心，对仅 16 个工具的项目有过度设计风险。取舍依据：甲方 v2.1 方案要求"数字人能力可配置、可按客户裁剪"，运行时可增删是硬需求，值得这层抽象。若中期确认无在线增删诉求，Phase 1-2 的 registry 仍是净收益（消灭三处重复分发），Phase 4 可缓做。
2. **lambda vs 函数引用**：`executors` 用 callable 而非 lambda 字符串，技能文件必须可 import；从 pip/git 安装的技能需保证其依赖在运行环境可用，`install` 接口要校验 import 成功后才落库 enabled。
3. **`stock_tools.py` / `digital_human_tools.py` 的去留**：Phase 3 后这两个文件仅剩系统提示词生成函数，可考虑把提示词也迁入技能后整文件删除；但 `digital_human_skill_prompt` 被 `stock_agent._system_prompt` 直接调用，删除前需确认所有调用点已切换。
4. **前端技能管理页**：Phase 4 需配套前端 `frontend/src/views/SkillsView.vue`，超出现有 `activePage` 路由机制的能力要在 `App.vue` 加一个 view 分支。
