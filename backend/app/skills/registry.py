"""技能注册中心 —— 进程内单例，负责技能的发现、注册、查询、执行与热加载。

设计目标见 docs/skill-system-tech-design.md。当前为 Phase 1：注册中心已建立并在应用
启动时 discover()，但 run_agent_events 仍走原 stock_tools / digital_human_tools 的
if/elif 分发链；本中心只注册、不接管，确保现有对话行为零变化。Phase 2 才切换分发。

技能契约：每个技能模块导出模块级 ``SKILL`` 字典，字段见 docs 第四节。本模块只识别
``SKILL``，不要求技能文件 import 本注册中心（避免循环）。
"""

from __future__ import annotations

import hashlib
import importlib
import pkgutil
from pathlib import Path
from typing import Any, Callable, Sequence

from backend.app.core.applog import get_logger
from backend.app.skills.manifests import (
    CORE_SKILL_MANIFESTS,
    LEGACY_SKILL_ALIASES,
    SkillManifest,
    load_core_skill_manifests,
)

logger = get_logger("skills")

_BUILTIN_SUBPKG = "_builtin"  # 内置安全关键技能子包（run_sql 等），Phase 2/3 引入
_PROGRESSIVE_INSTRUCTION_BUDGET = 24_000


def classify_research_intent(question: str) -> str:
    """Classify high-value research intents without spending another model call."""

    normalized = (question or "").strip().lower()
    switch_terms = ("换仓", "调仓", "卖掉", "清仓后买", "卖出后买", "换成")
    position_terms = ("持仓", "成本", "浮亏", "浮盈", "仓位", "套牢")
    action_terms = (
        "怎么办",
        "怎么操作",
        "操作方案",
        "周一",
        "明天",
        "下个交易日",
        "清仓",
        "减仓",
        "补仓",
        "止损",
        "止盈",
    )
    full_terms = ("全面分析", "完整分析", "深度分析", "全维", "基本面", "估值", "资金面")
    if any(term in normalized for term in switch_terms):
        return "switch_decision"
    if any(term in normalized for term in position_terms) and any(
        term in normalized for term in action_terms
    ):
        return "position_action_full" if any(term in normalized for term in full_terms) else "position_action"
    if any(term in normalized for term in full_terms):
        return "full_research"
    return "generic"


def _intent_skill_pack(intent: str) -> tuple[str, ...]:
    base = (
        "position-decision-support",
        "position-risk-review",
        "market-snapshot",
        "event-intelligence",
        "technical-analysis",
        "research-discipline",
        "financial-statement-analysis",
        "capital-flow-analysis",
        "company-deep-research",
        "data-routing",
    )
    if intent in {"switch_decision", "position_action_full"}:
        return (
            *base,
            "portfolio-risk-analysis",
            "knowledge-citation",
        )
    if intent == "full_research":
        return (
            "company-deep-research",
            "data-routing",
            "market-snapshot",
            "event-intelligence",
            "financial-statement-analysis",
            "technical-analysis",
            "capital-flow-analysis",
            "research-discipline",
            "knowledge-citation",
            "report-generation",
        )
    return base if intent == "position_action" else ()


def _intent_skill_limit(intent: str) -> int:
    if intent in {"switch_decision", "position_action_full", "full_research"}:
        return 12
    if intent == "position_action":
        return 10
    return 8


class SkillRegistry:
    """技能注册中心（进程内单例）。"""

    def __init__(self) -> None:
        self._skills: dict[str, dict] = {}                       # name -> SKILL
        self._tool_to_skill: dict[str, str] = {}                  # tool_name -> skill_name
        self._tool_executors: dict[str, Callable[[dict], dict]] = {}
        self._module_paths: list[str] = []                       # 已成功加载的技能模块路径（热加载用）
        self._manifests: dict[str, SkillManifest] = {
            item.name: item for item in CORE_SKILL_MANIFESTS
        }

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
        chat_mode: str | None = None,
        role_code: str | None = None,
        enabled: set[str] | None = None,
    ) -> list[str]:
        """收集匹配技能注入系统提示词的行为指令。

        过滤条件与 get_tools_for 对称（chat_mode + role + enabled），保证"下发了工具就
        下发对应规则"：某技能在当前模式/角色不可用时，其规则也不会污染提示词。
        chat_mode=None 表示不按模式过滤（兼容旧调用）。
        """
        instructions: list[str] = []
        for name, skill in self._skills.items():
            if enabled is not None and name not in enabled:
                continue
            if chat_mode is not None and chat_mode not in skill.get("chat_modes", []):
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

    def resolve_manifests(
        self,
        question: str,
        chat_mode: str,
        role_code: str | None = None,
        *,
        limit: int | None = None,
        selected_names: Sequence[str] | None = None,
    ) -> list[SkillManifest]:
        """Select a deterministic capability set sized to the task."""

        normalized = (question or "").lower()
        intent = classify_research_intent(question)
        requested_names = self.validate_manifest_selection(
            selected_names or (), chat_mode, role_code
        )
        effective_limit = max(
            1,
            min(max(limit or _intent_skill_limit(intent), len(requested_names)), 12),
        )
        eligible: dict[str, SkillManifest] = {
            manifest.name: manifest
            for manifest in self._manifests.values()
            if chat_mode in manifest.chat_modes
            and (not manifest.roles or role_code in manifest.roles)
        }
        selected: list[SkillManifest] = []
        for name in requested_names:
            selected.append(eligible[name])
        for name in _intent_skill_pack(intent):
            manifest = eligible.get(name)
            if manifest is not None and manifest not in selected:
                selected.append(manifest)

        ranked: list[tuple[int, int, SkillManifest]] = []
        for index, manifest in enumerate(eligible.values()):
            keyword_score = sum(3 for keyword in manifest.keywords if keyword.lower() in normalized)
            if manifest.name in normalized:
                keyword_score += 5
            role_score = 2 if role_code in manifest.default_roles else 0
            score = keyword_score + role_score
            if score:
                ranked.append((score, -index, manifest))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        for _, _, manifest in ranked:
            if manifest not in selected:
                selected.append(manifest)
            if len(selected) >= effective_limit:
                break
        # Generic questions can have fewer keyword hits than the configured
        # baseline. Fill deterministically with role-default skills first so
        # every Agent receives the promised minimum capability pack.
        for manifest in eligible.values():
            if role_code not in manifest.default_roles or manifest in selected:
                continue
            selected.append(manifest)
            if len(selected) >= effective_limit:
                break
        for manifest in eligible.values():
            if manifest in selected:
                continue
            selected.append(manifest)
            if len(selected) >= effective_limit:
                break
        return selected[:effective_limit]

    def build_progressive_context(
        self,
        selected: list[SkillManifest],
        chat_mode: str,
        role_code: str | None,
    ) -> str:
        available = [
            item
            for item in self._manifests.values()
            if chat_mode in item.chat_modes and (not item.roles or role_code in item.roles)
        ]
        if not available:
            return ""
        selected_names = {item.name for item in selected}
        lines = [
            "## Agent Skills",
            "以下技能按 Agent Skills 规范渐进加载。已激活技能的完整 SKILL.md "
            "已进入上下文；其他技能只暴露名称和触发描述。需要细节时调用 load_skill，"
            "需要引用资料时再传 resource_path。",
            "### 已激活",
        ]
        instruction_chars = 0
        deferred_selected: list[SkillManifest] = []
        for item in selected:
            if (
                instruction_chars
                and instruction_chars + len(item.instructions)
                > _PROGRESSIVE_INSTRUCTION_BUDGET
            ):
                deferred_selected.append(item)
                continue
            lines.append(
                f'\n<skill name="{item.name}" version="{item.version}">\n'
                f"{item.instructions}\n"
                "</skill>"
            )
            instruction_chars += len(item.instructions)
        if deferred_selected:
            lines.append("### 已选择但按需加载")
            for item in deferred_selected:
                lines.append(
                    f"- {item.name}：{item.summary}。需要完整规则时调用 load_skill。"
                )
        lines.append("### 可按需加载")
        for item in available:
            if item.name not in selected_names:
                lines.append(f"- {item.name}：{item.summary}")
        return "\n".join(lines)

    @staticmethod
    def _canonical_manifest_name(name: str) -> str:
        normalized = (name or "").strip()
        return LEGACY_SKILL_ALIASES.get(normalized, normalized)

    def load_manifest(self, name: str, resource_path: str | None = None) -> dict[str, Any]:
        canonical_name = self._canonical_manifest_name(name)
        manifest = self._manifests.get(canonical_name)
        if manifest is None:
            return {"error": f"Unknown skill: {name}", "available": sorted(self._manifests)}
        if resource_path:
            candidate = (manifest.skill_dir / resource_path).resolve()
            skill_root = manifest.skill_dir.resolve()
            if candidate == skill_root or skill_root not in candidate.parents:
                return {"error": "resource_path escapes the skill directory"}
            if not candidate.is_file():
                return {
                    "error": f"Unknown resource: {resource_path}",
                    "available_resources": list(manifest.resources),
                }
            relative = candidate.relative_to(skill_root)
            if len(relative.parts) > 2:
                return {"error": "resources must be referenced one level deep from SKILL.md"}
            try:
                content = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return {"error": "resource is not readable UTF-8 text"}
            return {
                "name": manifest.name,
                "resource_path": str(relative).replace("\\", "/"),
                "content": content,
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            }
        return {
            "name": manifest.name,
            "display_name": manifest.display_name,
            "summary": manifest.summary,
            "description": manifest.summary,
            "instructions": manifest.instructions,
            "professional_body": manifest.instructions,
            "content_sha256": manifest.content_sha256,
            "version": manifest.version,
            "category": manifest.category,
            "risk_level": manifest.risk_level,
            "compatibility": manifest.compatibility,
            "license": manifest.license,
            "metadata": manifest.metadata,
            "allowed_tools": list(manifest.allowed_tools),
            "resources": list(manifest.resources),
            "output_contract": list(manifest.output_contract),
            "criteria": [
                {
                    "code": item.code,
                    "description": item.description,
                    "required": item.required,
                    "min_evidence": item.min_evidence,
                    "tool_names": list(item.tool_names),
                    "validation_profile": item.validation_profile,
                    "max_age_days": item.max_age_days,
                }
                for item in manifest.criteria
            ],
            "tool_names": list(manifest.tool_names),
        }

    def manifests_by_names(self, names: list[str] | tuple[str, ...]) -> list[SkillManifest]:
        manifests: list[SkillManifest] = []
        for name in names:
            manifest = self._manifests.get(self._canonical_manifest_name(name))
            if manifest and manifest not in manifests:
                manifests.append(manifest)
        return manifests

    def validate_manifest_selection(
        self,
        names: Sequence[str],
        chat_mode: str,
        role_code: str | None = None,
        *,
        max_selected: int = 8,
    ) -> list[str]:
        """Canonicalize and validate explicitly selected skills for one chat turn."""

        canonical_names: list[str] = []
        for raw_name in names:
            canonical_name = self._canonical_manifest_name(str(raw_name))
            if not canonical_name or canonical_name in canonical_names:
                continue
            canonical_names.append(canonical_name)
        if len(canonical_names) > max_selected:
            raise ValueError(f"每轮最多选择 {max_selected} 个技能")
        for name in canonical_names:
            manifest = self._manifests.get(name)
            if manifest is None:
                raise ValueError(f"未知技能：{name}")
            if chat_mode not in manifest.chat_modes:
                raise ValueError(f"技能 {name} 不适用于当前对话模式")
            if manifest.roles and role_code not in manifest.roles:
                raise ValueError(f"技能 {name} 不适用于当前数字人")
        return canonical_names

    def list_manifests(self) -> list[dict[str, Any]]:
        manifests: list[dict[str, Any]] = []
        for item in self._manifests.values():
            manifests.append(
                {
                    "name": item.name,
                    "display_name": item.display_name,
                    "summary": item.summary,
                    "description": item.summary,
                    "version": item.version,
                    "category": item.category,
                    "risk_level": item.risk_level,
                    "compatibility": item.compatibility,
                    "license": item.license,
                    "chat_modes": list(item.chat_modes),
                    "roles": list(item.roles),
                    "tool_names": list(item.tool_names),
                    "allowed_tools": list(item.allowed_tools),
                    "resources": list(item.resources),
                    "output_contract": list(item.output_contract),
                    "professional_body_available": bool(item.instructions),
                    "content_sha256": item.content_sha256,
                    "criteria": [
                        {
                            "code": criterion.code,
                            "description": criterion.description,
                            "required": criterion.required,
                            "min_evidence": criterion.min_evidence,
                            "validation_profile": criterion.validation_profile,
                            "max_age_days": criterion.max_age_days,
                        }
                        for criterion in item.criteria
                    ],
                }
            )
        return manifests

    def criterion_codes_for_tool(
        self, tool_name: str, selected_skill_names: list[str] | tuple[str, ...]
    ) -> list[str]:
        """Keep contract metadata queryable without enabling runtime evidence audits."""

        codes: list[str] = []
        for skill_name in selected_skill_names:
            manifest = self._manifests.get(self._canonical_manifest_name(skill_name))
            if manifest is None:
                continue
            for criterion in manifest.criteria:
                if tool_name in criterion.tool_names:
                    codes.append(criterion.code)
        return codes

    def skill_names(self) -> list[str]:
        return list(self._skills.keys())

    def all_tool_names(self) -> frozenset[str]:
        return frozenset(self._tool_executors.keys())

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
        self._manifests = {
            item.name: item for item in load_core_skill_manifests()
        }


skill_registry = SkillRegistry()
