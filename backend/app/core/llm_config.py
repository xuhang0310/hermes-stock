"""活跃 LLM 模型配置的纯函数查询——从 stock_agent.py 中提取出来的共享依赖。

避免 memory / position_agent / position_import / prompt_recommendations
四个模块循环导入 900+ 行的 agent 文件。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.core import runtime_overrides
from backend.app.core.database import SessionLocal
from backend.app.core.models import AiChatSession, LlmModelConfig
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


def resolve_chat_model_config_ids(model_config_id: int | None = None) -> list[int]:
    """Return enabled chat models in failover order.

    The requested (or active) model stays first.  Models on other platforms
    are tried before another model on the same platform so a provider-level
    entitlement or credential failure can recover without repeating the same
    failing integration.
    """
    with SessionLocal() as session:
        rows = session.scalars(
            select(LlmModelConfig)
            .options(selectinload(LlmModelConfig.platform))
            .where(
                LlmModelConfig.status == "enabled",
            )
            .order_by(
                LlmModelConfig.is_active.desc(),
                LlmModelConfig.updated_at.desc(),
                LlmModelConfig.id.desc(),
            )
        ).all()
        available = [
            row
            for row in rows
            if row.platform is None or row.platform.status == "enabled"
        ]
        if not available:
            return []

        primary = None
        if model_config_id:
            primary = next((row for row in available if row.id == model_config_id), None)
        if primary is None:
            primary = next((row for row in available if row.is_active), available[0])

        remaining = [row for row in available if row.id != primary.id]
        other_platforms = [
            row for row in remaining if row.platform_id != primary.platform_id
        ]
        same_platform = [
            row for row in remaining if row.platform_id == primary.platform_id
        ]
        return [primary.id, *[row.id for row in other_platforms], *[row.id for row in same_platform]]


def persist_chat_session_model(*, session_id: int, user_id: int, model_config_id: int) -> bool:
    """Update an owned session's explicit model after failover. Null follows the default."""
    with SessionLocal() as session:
        chat_session = session.scalar(
            select(AiChatSession)
            .where(
                AiChatSession.id == session_id,
                AiChatSession.user_id == user_id,
                AiChatSession.status != "deleted",
                AiChatSession.model_config_id.is_not(None),
            )
            .with_for_update()
            .limit(1)
        )
        model = session.scalar(
            select(LlmModelConfig)
            .options(selectinload(LlmModelConfig.platform))
            .where(
                LlmModelConfig.id == model_config_id,
                LlmModelConfig.status == "enabled",
            )
            .limit(1)
        )
        if (
            chat_session is None
            or model is None
            or (model.platform is not None and model.platform.status != "enabled")
        ):
            return False
        chat_session.model_config_id = model_config_id
        session.commit()
        return True


def model_supports_vision(model_name: str | None) -> bool:
    """Return whether a configured model is known to accept image content."""
    normalized = (model_name or "").strip().lower()
    return any(
        normalized == prefix or normalized.startswith(f"{prefix}-")
        for prefix in _VISION_MODEL_PREFIXES
    )


def resolve_model_config_ids_by_priority(model_names: tuple[str, ...]) -> list[int]:
    """Return one enabled model configuration for each preferred model name.

    The returned IDs preserve ``model_names`` order.  Duplicate configurations
    for the same model are resolved in favour of the active, most recently
    updated row.  Disabled model platforms are never selected.
    """
    normalized_names = tuple(
        name.strip().lower()
        for name in model_names
        if name and name.strip()
    )
    if not normalized_names:
        return []

    with SessionLocal() as session:
        candidates = session.scalars(
            select(LlmModelConfig)
            .options(selectinload(LlmModelConfig.platform))
            .where(
                LlmModelConfig.status != "disabled",
            )
            .order_by(
                LlmModelConfig.is_active.desc(),
                LlmModelConfig.updated_at.desc(),
                LlmModelConfig.id.desc(),
            )
        ).all()
        available = [
            row
            for row in candidates
            if row.platform is None or row.platform.status != "disabled"
        ]

        resolved: list[int] = []
        for preferred_name in normalized_names:
            preferred = next(
                (
                    row
                    for row in available
                    if (row.model_name or "").strip().lower() == preferred_name
                ),
                None,
            )
            if preferred is not None:
                resolved.append(preferred.id)
        return resolved


def resolve_position_agent_model_config_id(model_config_id: int | None = None) -> int | None:
    """Use the chat default for text entry and validate explicit model choices."""
    candidates = resolve_chat_model_config_ids(model_config_id)
    if model_config_id is not None and model_config_id not in candidates:
        raise RuntimeError("所选模型配置不存在或已停用。")
    return candidates[0] if candidates else None


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
            query.where(
                LlmModelConfig.status != "disabled",
            ).order_by(
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


# 推理强度分级（对齐行业推理模型约定：OpenAI reasoning_effort / AgentScope 引擎同名参数；
# auto=不下发任何推理参数，跟随服务商默认）
REASONING_LEVELS = ("off", "low", "medium", "high", "xhigh", "max")

# OpenAI 系顶层 reasoning_effort 透传映射：off→none；OpenAI 无 max 档，收敛到 xhigh
_OPENAI_EFFORT = {
    "off": "none",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "xhigh",
}


def reasoning_family(llm_config: dict) -> str:
    """按服务商/模型判定推理强度协议族。

    - ``"qwen"``：通义/百炼（DashScope）系，含 OpenAI 兼容端点上的 qwen 模型，
      只认请求体里的 ``enable_thinking``（布尔），不认 reasoning_effort；
    - ``"openai"``：其余（OpenAI 推理系列、DeepSeek 及一般 OpenAI 兼容端点），
      接收顶层 ``reasoning_effort`` 分级。
    """
    provider = str(llm_config.get("provider_name") or "").lower()
    base_url = str(llm_config.get("base_url") or "").lower()
    model = str(llm_config.get("model") or "").lower()
    qwen_providers = {
        "dashscope", "qwen", "tongyi", "aliyun",
        "通义", "百炼", "阿里云", "阿里",
    }
    if provider in qwen_providers or "dashscope" in base_url:
        return "qwen"
    if model.startswith("qwen"):
        return "qwen"
    return "openai"


def reasoning_params(llm_config: dict) -> dict:
    """推理强度配置 → 请求参数字典。

    返回 ``{"reasoning_effort": str | None, "enable_thinking": bool | None}``，
    auto（或非法值）时两者均为 None，即不下发任何推理参数。
    qwen 族把级别映射成 enable_thinking（该族思考是布尔开关，off=关闭）；
    openai 族透传顶层 reasoning_effort（off→none、max→xhigh；具体可选值因模型
    而异，若某模型拒绝某级别，调低或改回 auto 即可）。
    """
    level = str(llm_config.get("reasoning_effort") or "auto").lower()
    if level not in REASONING_LEVELS:
        return {"reasoning_effort": None, "enable_thinking": None}
    if reasoning_family(llm_config) == "qwen":
        return {"reasoning_effort": None, "enable_thinking": level != "off"}
    return {"reasoning_effort": _OPENAI_EFFORT[level], "enable_thinking": None}


def active_llm_config(model_config_id: int | None = None) -> dict:
    """获取当前活跃 LLM 模型配置。

    优先取指定 model_config_id；未指定时取 is_active 且 enabled 的记录；
    都没有则回退到环境变量。

    Args:
        model_config_id: 可选，指定某个模型配置 ID
    Returns:
        包含 id/display_name/provider_name/base_url/api_key/model/temperature/max_steps/reasoning_effort 的字典
    """
    fallback = {
        "id": None,
        "display_name": "环境变量默认模型",
        "provider_name": "环境变量",
        "base_url": settings.llm_base_url,
        "api_key": settings.llm_api_key,
        "model": settings.llm_model,
        "temperature": 0.2,
        # 运行时覆盖优先于环境变量（白名单机制，见 core.runtime_overrides）
        "max_steps": runtime_overrides.agent_max_steps(settings.agent_max_steps),
        # 推理强度：auto=不下发推理参数，跟随服务商默认
        "reasoning_effort": "auto",
    }
    with SessionLocal() as session:
        query = select(LlmModelConfig).options(selectinload(LlmModelConfig.platform))
        if model_config_id:
            row = session.scalar(
                query.where(
                    LlmModelConfig.id == model_config_id,
                    LlmModelConfig.status != "disabled",
                ).limit(1)
            )
            if row is None:
                raise RuntimeError("所选模型配置不存在或已停用。")
        else:
            row = session.scalar(
                query.where(
                    LlmModelConfig.is_active.is_(True),
                    LlmModelConfig.status == "enabled",
                )
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
            # 运行时覆盖（如已设置）优先于模型配置与环境变量
            "max_steps": runtime_overrides.agent_max_steps(
                int(row.max_steps or fallback["max_steps"])
            ),
            "reasoning_effort": str(row.reasoning_effort or "auto"),
        }
