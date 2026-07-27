"""活跃 LLM 模型配置的纯函数查询——从 stock_agent.py 中提取出来的共享依赖。

避免 memory / position_agent / position_import / prompt_recommendations
四个模块循环导入 900+ 行的 agent 文件。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.core.database import SessionLocal
from backend.app.core.models import LlmModelConfig
from backend.app.core.settings import settings


_VISION_MODEL_PREFIXES = (
    "qwen3.7-plus",
    "qwen3.6-plus",
    "qwen3.5-plus",
    "qwen3-vl",
    "qwen2.5-vl",
    "qwen-vl",
    "qwen3-omni",
    "qwen-omni",
    "qvq",
    "kimi-k2.5",
    "k3",
)

_POSITION_AGENT_MODEL_PRIORITY = ("qwen3.7-plus", "k3")


def model_supports_vision(model_name: str | None) -> bool:
    """Return whether a configured model is known to accept image content."""
    normalized = (model_name or "").strip().lower()
    return any(
        normalized == prefix or normalized.startswith(f"{prefix}-")
        for prefix in _VISION_MODEL_PREFIXES
    )


def resolve_position_agent_model_config_id(model_config_id: int | None = None) -> int | None:
    """Choose the position-entry model, preferring Qwen3.7 Plus then k3."""
    with SessionLocal() as session:
        query = select(LlmModelConfig).options(selectinload(LlmModelConfig.platform))
        if model_config_id:
            selected = session.scalar(
                query.where(
                    LlmModelConfig.id == model_config_id,
                    LlmModelConfig.status != "disabled",
                ).limit(1)
            )
            if selected is None or (
                selected.platform is not None and selected.platform.status == "disabled"
            ):
                raise RuntimeError("所选模型配置不存在或已停用。")
            return selected.id

        candidates = session.scalars(
            query.where(LlmModelConfig.status != "disabled").order_by(
                LlmModelConfig.is_active.desc(),
                LlmModelConfig.updated_at.desc(),
                LlmModelConfig.id.desc(),
            )
        ).all()
        candidates = [
            row
            for row in candidates
            if row.platform is None or row.platform.status != "disabled"
        ]
        for preferred_name in _POSITION_AGENT_MODEL_PRIORITY:
            preferred = next(
                (
                    row
                    for row in candidates
                    if (row.model_name or "").strip().lower() == preferred_name
                ),
                None,
            )
            if preferred is not None:
                return preferred.id
        if candidates:
            return candidates[0].id
        return None


def resolve_vision_model_config_id(model_config_id: int | None = None) -> int | None:
    """Resolve an enabled vision model, preferring the requested model/platform."""
    with SessionLocal() as session:
        query = select(LlmModelConfig).options(selectinload(LlmModelConfig.platform))
        selected = None
        if model_config_id:
            selected = session.scalar(
                query.where(
                    LlmModelConfig.id == model_config_id,
                    LlmModelConfig.status != "disabled",
                ).limit(1)
            )
            if selected is None:
                raise RuntimeError("所选模型配置不存在或已停用。")
        else:
            selected = session.scalar(
                query.where(
                    LlmModelConfig.is_active.is_(True),
                    LlmModelConfig.status == "enabled",
                )
                .order_by(LlmModelConfig.updated_at.desc(), LlmModelConfig.id.desc())
                .limit(1)
            )

        if selected is not None and model_supports_vision(selected.model_name):
            return selected.id

        candidates = session.scalars(
            query.where(LlmModelConfig.status != "disabled").order_by(
                LlmModelConfig.is_active.desc(),
                LlmModelConfig.updated_at.desc(),
                LlmModelConfig.id.desc(),
            )
        ).all()
        vision_candidates = [
            row
            for row in candidates
            if model_supports_vision(row.model_name)
            and (row.platform is None or row.platform.status != "disabled")
        ]
        if selected is not None and selected.platform_id is not None:
            same_platform = next(
                (
                    row
                    for row in vision_candidates
                    if row.platform_id == selected.platform_id
                ),
                None,
            )
            if same_platform is not None:
                return same_platform.id
        if vision_candidates:
            return vision_candidates[0].id
        if selected is None and model_supports_vision(settings.llm_model):
            return None
        raise RuntimeError(
            "当前没有配置支持图片理解的模型，请先启用 Qwen3.7 Plus 或其他视觉模型。"
        )


def active_llm_config(model_config_id: int | None = None) -> dict:
    """获取当前活跃 LLM 模型配置。

    优先取指定 model_config_id；未指定时取 is_active 且 enabled 的记录；
    都没有则回退到环境变量。

    Args:
        model_config_id: 可选，指定某个模型配置 ID
    Returns:
        包含 id/display_name/provider_name/base_url/api_key/model/temperature/max_steps 的字典
    """
    fallback = {
        "id": None,
        "display_name": "环境变量默认模型",
        "provider_name": "环境变量",
        "base_url": settings.llm_base_url,
        "api_key": settings.llm_api_key,
        "model": settings.llm_model,
        "temperature": 0.2,
        "max_steps": settings.agent_max_steps,
    }
    with SessionLocal() as session:
        query = select(LlmModelConfig).options(selectinload(LlmModelConfig.platform))
        if model_config_id:
            row = session.scalar(query.where(LlmModelConfig.id == model_config_id, LlmModelConfig.status != "disabled").limit(1))
            if row is None:
                raise RuntimeError("所选模型配置不存在或已停用。")
        else:
            row = session.scalar(
                query.where(LlmModelConfig.is_active.is_(True), LlmModelConfig.status == "enabled")
                .order_by(LlmModelConfig.updated_at.desc(), LlmModelConfig.id.desc())
                .limit(1)
            )
            if row is None:
                return fallback

        platform = row.platform
        return {
            "id": row.id,
            "display_name": row.display_name,
            "provider_name": platform.name if platform else row.provider_name,
            "base_url": (platform.base_url if platform else row.base_url) or fallback["base_url"],
            "api_key": (platform.api_key if platform and platform.api_key else row.api_key) or fallback["api_key"],
            "model": row.model_name or fallback["model"],
            "temperature": float(row.temperature if row.temperature is not None else fallback["temperature"]),
            "max_steps": int(row.max_steps or fallback["max_steps"]),
        }
