"""Agent Skills specification compatible skill catalog.

Each user-facing skill lives in ``catalog/<skill-name>/SKILL.md``.  The Markdown
file is the canonical agent instruction.  ``contract.json`` is an optional
Hermes runtime extension for deterministic routing and evidence acceptance.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


CATALOG_DIR = Path(__file__).resolve().parent / "catalog"
_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SUPPORTED_FRONTMATTER = {
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
}


@dataclass(frozen=True)
class SkillCriterion:
    code: str
    description: str
    tool_names: tuple[str, ...]
    min_evidence: int = 1
    required: bool = True
    validation_profile: str = "substantive"
    max_age_days: int | None = None


@dataclass(frozen=True)
class SkillManifest:
    name: str
    display_name: str
    summary: str
    instructions: str
    keywords: tuple[str, ...]
    chat_modes: tuple[str, ...]
    roles: tuple[str, ...] = ()
    default_roles: tuple[str, ...] = ()
    tool_names: tuple[str, ...] = ()
    criteria: tuple[SkillCriterion, ...] = ()
    version: str = "1.0.0"
    category: str = "other"
    risk_level: str = "low"
    output_contract: tuple[str, ...] = ()
    compatibility: str = ""
    license: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()
    skill_dir: Path = Path()
    resources: tuple[str, ...] = ()
    content_sha256: str = ""


def _split_skill_markdown(text: str, path: Path) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        raise ValueError(f"{path}: SKILL.md must start with YAML frontmatter")
    parts = text.split("---", 2)
    if len(parts) != 3:
        raise ValueError(f"{path}: SKILL.md frontmatter is not closed")
    raw = yaml.safe_load(parts[1]) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: frontmatter must be a YAML mapping")
    unknown = set(raw) - _SUPPORTED_FRONTMATTER
    if unknown:
        raise ValueError(f"{path}: unsupported frontmatter fields: {sorted(unknown)}")
    return raw, parts[2].strip()


def _string_tuple(value: Any, field_name: str, path: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{path}: contract.{field_name} must be a string array")
    return tuple(item.strip() for item in value if item.strip())


def _load_contract(skill_dir: Path) -> dict[str, Any]:
    path = skill_dir / "contract.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: invalid contract.json: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: contract must be a JSON object")
    return payload


def _load_skill_dir(skill_dir: Path) -> SkillManifest:
    skill_path = skill_dir / "SKILL.md"
    text = skill_path.read_text(encoding="utf-8")
    frontmatter, body = _split_skill_markdown(text, skill_path)
    name = str(frontmatter.get("name") or "").strip()
    description = str(frontmatter.get("description") or "").strip()
    if not _NAME_PATTERN.fullmatch(name) or len(name) > 64:
        raise ValueError(f"{skill_path}: invalid skill name {name!r}")
    if name != skill_dir.name:
        raise ValueError(f"{skill_path}: name must match parent directory")
    if not description or len(description) > 1024:
        raise ValueError(f"{skill_path}: description must contain 1-1024 characters")
    if not body:
        raise ValueError(f"{skill_path}: Markdown instructions are required")

    metadata = frontmatter.get("metadata") or {}
    if not isinstance(metadata, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in metadata.items()
    ):
        raise ValueError(f"{skill_path}: metadata must map strings to strings")
    compatibility = str(frontmatter.get("compatibility") or "").strip()
    if len(compatibility) > 500:
        raise ValueError(f"{skill_path}: compatibility exceeds 500 characters")
    allowed_tools_value = frontmatter.get("allowed-tools") or ""
    if not isinstance(allowed_tools_value, str):
        raise ValueError(f"{skill_path}: allowed-tools must be a space-separated string")
    allowed_tools = tuple(item for item in allowed_tools_value.split() if item)

    contract = _load_contract(skill_dir)
    tool_names = _string_tuple(contract.get("tool_names"), "tool_names", skill_path)
    if allowed_tools and set(tool_names) - set(allowed_tools):
        raise ValueError(
            f"{skill_path}: contract tools must be declared in allowed-tools: "
            f"{sorted(set(tool_names) - set(allowed_tools))}"
        )
    criteria: list[SkillCriterion] = []
    for index, item in enumerate(contract.get("criteria") or []):
        if not isinstance(item, dict):
            raise ValueError(f"{skill_path}: criteria[{index}] must be an object")
        criterion_tools = _string_tuple(
            item.get("tool_names"), f"criteria[{index}].tool_names", skill_path
        )
        if set(criterion_tools) - set(tool_names):
            raise ValueError(f"{skill_path}: criterion tools must belong to contract tools")
        criteria.append(
            SkillCriterion(
                code=str(item.get("code") or "").strip(),
                description=str(item.get("description") or "").strip(),
                tool_names=criterion_tools,
                min_evidence=max(1, int(item.get("min_evidence", 1))),
                required=bool(item.get("required", True)),
                validation_profile=str(item.get("validation_profile") or "substantive"),
                max_age_days=(
                    int(item["max_age_days"])
                    if item.get("max_age_days") is not None
                    else None
                ),
            )
        )
    if any(not item.code or not item.description for item in criteria):
        raise ValueError(f"{skill_path}: every criterion needs code and description")

    resources = tuple(
        sorted(
            str(path.relative_to(skill_dir)).replace("\\", "/")
            for path in skill_dir.rglob("*")
            if path.is_file() and path.name not in {"SKILL.md", "contract.json"}
        )
    )
    return SkillManifest(
        name=name,
        display_name=metadata.get("hermes-display-name", name),
        summary=description,
        instructions=body,
        keywords=_string_tuple(contract.get("triggers"), "triggers", skill_path),
        chat_modes=_string_tuple(contract.get("chat_modes"), "chat_modes", skill_path),
        roles=_string_tuple(contract.get("roles"), "roles", skill_path),
        default_roles=_string_tuple(
            contract.get("default_roles"), "default_roles", skill_path
        ),
        tool_names=tool_names,
        criteria=tuple(criteria),
        version=metadata.get("hermes-version", "1.0.0"),
        category=metadata.get("hermes-category", "other"),
        risk_level=metadata.get("hermes-risk-level", "low"),
        output_contract=_string_tuple(
            contract.get("output_contract"), "output_contract", skill_path
        ),
        compatibility=compatibility,
        license=str(frontmatter.get("license") or "").strip(),
        metadata=dict(metadata),
        allowed_tools=allowed_tools,
        skill_dir=skill_dir,
        resources=resources,
        content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def load_core_skill_manifests(catalog_dir: Path = CATALOG_DIR) -> tuple[SkillManifest, ...]:
    if not catalog_dir.exists():
        return ()
    manifests = [
        _load_skill_dir(path)
        for path in sorted(catalog_dir.iterdir())
        if path.is_dir() and (path / "SKILL.md").exists()
    ]
    names = [item.name for item in manifests]
    if len(names) != len(set(names)):
        raise ValueError("duplicate Agent Skill names")
    return tuple(manifests)


CORE_SKILL_MANIFESTS = load_core_skill_manifests()

# Existing sessions and clients used underscore identifiers before the catalog
# adopted the Agent Skills naming convention. Keep reads compatible.
LEGACY_SKILL_ALIASES = {
    "market_snapshot": "market-snapshot",
    "fundamental_research": "financial-statement-analysis",
    "news_and_events": "event-intelligence",
    "technical_analysis": "technical-analysis",
    "capital_flow": "capital-flow-analysis",
    "stock_research": "company-deep-research",
    "position_risk": "position-risk-review",
    "watchlist_review": "watchlist-research",
    "portfolio_risk": "portfolio-risk-analysis",
    "knowledge_citation": "knowledge-citation",
    "report_artifacts": "report-generation",
    "code_and_backtest": "code-and-backtest",
}


LOAD_SKILL_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": (
            "按名称加载一个 Agent Skill 的完整 SKILL.md；需要详细资料时，"
            "再通过 resource_path 加载该技能目录内的 references 或其他资源。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "description": "技能名称"},
                "resource_path": {
                    "type": "string",
                    "description": "可选，相对技能根目录的资源路径，例如 references/red-flags.md",
                },
            },
            "required": ["skill_name"],
        },
    },
}
