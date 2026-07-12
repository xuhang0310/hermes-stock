"""技能（Skill）系统 —— 可插拔、自动发现的智能体工具包。

每个技能是一个 Python 模块，模块级导出 SKILL 字典（schema + executor + roles +
prompt_instructions）。SkillRegistry 在应用启动时扫描本包并自动注册，运行时提供统一的
工具查询与分发。

当前为 Phase 1：注册中心已建立并接入 lifespan，但尚未接管 run_agent_events 的分发链
（stock_tools / digital_human_tools 的 if/elif 仍为运行时实际路径）。Phase 2 才切换分发。
详见 docs/skill-system-tech-design.md。
"""

from backend.app.skills.registry import SkillRegistry, skill_registry

__all__ = ["SkillRegistry", "skill_registry"]
