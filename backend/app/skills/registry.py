"""技能注册中心 —— 进程内单例，负责技能的发现、注册、查询、执行与热加载。

设计目标见 docs/skill-system-tech-design.md。当前为 Phase 1：注册中心已建立并在应用
启动时 discover()，但 run_agent_events 仍走原 stock_tools / digital_human_tools 的
if/elif 分发链；本中心只注册、不接管，确保现有对话行为零变化。Phase 2 才切换分发。

技能契约：每个技能模块导出模块级 ``SKILL`` 字典，字段见 docs 第四节。本模块只识别
``SKILL``，不要求技能文件 import 本注册中心（避免循环）。
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Any, Callable

from backend.app.core.applog import get_logger

logger = get_logger("skills")

_BUILTIN_SUBPKG = "_builtin"  # 内置安全关键技能子包（run_sql 等），Phase 2/3 引入


class SkillRegistry:
    """技能注册中心（进程内单例）。"""

    def __init__(self) -> None:
        self._skills: dict[str, dict] = {}                       # name -> SKILL
        self._tool_to_skill: dict[str, str] = {}                  # tool_name -> skill_name
        self._tool_executors: dict[str, Callable[[dict], dict]] = {}
        self._module_paths: list[str] = []                       # 已成功加载的技能模块路径（热加载用）

    # ------------------------------------------------------------------ 发现
    def discover(self, package: str = "backend.app.skills") -> None:
        """扫描技能包，自动 import 并注册全部技能模块。

        单个技能加载失败不影响其他技能：记 warning 后跳过。可重复调用（reload 底层）。
        """
        self._skills.clear()
        self._tool_to_skill.clear()
        self._tool_executors.clear()
        self._module_paths.clear()

        pkg = self._import_package(package)
        if pkg is not None:
            self._scan_package(package, pkg)

        # 内置安全关键技能子包（不可卸载）—— 当前可能尚未创建，缺失则跳过
        builtin_path = f"{package}.{_BUILTIN_SUBPKG}"
        builtin = self._import_package(builtin_path)
        if builtin is not None:
            self._scan_package(builtin_path, builtin)

        logger.info(
            "技能发现完成：%d 个技能、%d 个工具", len(self._skills), len(self._tool_executors)
        )

    def _import_package(self, package: str):
        try:
            return importlib.import_module(package)
        except ModuleNotFoundError:
            return None  # 子包尚未创建，正常情况
        except Exception as exc:  # pragma: no cover - 防御性
            logger.warning("导入技能包 %s 失败: %s", package, exc)
            return None

    def _scan_package(self, package: str, pkg) -> None:
        pkg_file = getattr(pkg, "__file__", None)
        if not pkg_file:
            return
        pkg_dir = str(Path(pkg_file).parent)
        for module_info in pkgutil.iter_modules([pkg_dir]):
            if module_info.name.startswith("_"):
                continue  # 跳过 __init__、_builtin 子包入口、私有模块
            self._load_module(f"{package}.{module_info.name}")

    def _load_module(self, module_path: str) -> None:
        try:
            mod = importlib.import_module(module_path)
        except Exception as exc:
            logger.warning("技能模块加载失败 %s: %s", module_path, exc)
            return
        skill = getattr(mod, "SKILL", None)
        if skill is None:
            return  # 非技能模块（registry、_builtin 入口、辅助模块），静默跳过
        if not isinstance(skill, dict) or "name" not in skill:
            logger.warning("技能模块 %s 的 SKILL 非有效字典（缺 name），跳过", module_path)
            return
        try:
            self.register(skill)
        except ValueError as exc:
            logger.warning("技能注册失败 %s: %s", module_path, exc)
            return
        self._module_paths.append(module_path)

    def register(self, skill: dict) -> None:
        """注册单个技能。重名抛 ValueError（discover 已捕获并记 warning）。"""
        name = skill["name"]
        if name in self._skills:
            raise ValueError(f"重复技能名: {name}")
        self._skills[name] = skill
        for tool in skill.get("tools", []):
            tname = tool["function"]["name"]
            if tname in self._tool_to_skill:
                logger.warning(
                    "工具名 %s 在多个技能中重复（%s / %s），后者覆盖",
                    tname, self._tool_to_skill[tname], name,
                )
            self._tool_to_skill[tname] = name
        for tname, fn in skill.get("executors", {}).items():
            self._tool_executors[tname] = fn

    # ------------------------------------------------------------------ 查询
    def get_tools_for(
        self,
        chat_mode: str,
        role_code: str | None = None,
        enabled: set[str] | None = None,
    ) -> list[dict]:
        """按对话模式 / 角色 / 启用集合返回可用工具 schema 列表。"""
        tools: list[dict] = []
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

    def get_prompt_instructions(
        self,
        role_code: str | None = None,
        enabled: set[str] | None = None,
    ) -> list[str]:
        """收集匹配技能注入系统提示词的行为指令。"""
        instructions: list[str] = []
        for name, skill in self._skills.items():
            if enabled is not None and name not in enabled:
                continue
            roles = skill.get("roles", [])
            if roles and role_code not in roles:
                continue
            instructions.extend(skill.get("prompt_instructions", []))
        return instructions

    def list_skills(self) -> list[dict]:
        """前端技能列表 / 管理页 / 自检用（不含 executor 引用，可安全 JSON 化）。"""
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

    def skill_names(self) -> list[str]:
        return list(self._skills.keys())

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in self._tool_executors

    # ------------------------------------------------------------------ 执行
    def execute(self, tool_name: str, args: dict[str, Any] | None) -> dict:
        """执行工具。未知工具或执行异常均返回 {'error': ...}，与现有 dispatcher 一致。"""
        fn = self._tool_executors.get(tool_name)
        if fn is None:
            return {"error": f"Unknown tool: {tool_name}"}
        try:
            return fn(args or {})
        except Exception as exc:
            logger.warning("工具 %s 执行异常: %s", tool_name, exc)
            return {"error": f"tool {tool_name} failed: {exc}"}

    # ------------------------------------------------------------------ 热加载
    def reload(self, package: str = "backend.app.skills") -> None:
        """运行时重新发现并注册：先 reload 已加载模块（拾取文件改动），再 discover 新增。"""
        for path in list(self._module_paths):
            try:
                importlib.reload(importlib.import_module(path))
            except Exception as exc:
                logger.warning("热加载技能模块 %s 失败: %s", path, exc)
        self.discover(package)


skill_registry = SkillRegistry()
