from __future__ import annotations

import asyncio
import json
import time
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from .auth import AuthContext, require_permission
from .database import SessionLocal, get_db
from .models import (
    AiChatMessage,
    AiPromptRecommendation,
    AiChatSession,
    DataSourceConfig,
    DigitalHumanProfile,
    LlmModelConfig,
    LlmModelPlatform,
)
from .prompt_recommendations import (
    active_recommendations,
    dismiss_recommendation,
    recommendation_out,
    refresh_recommendations_for_user_profile,
)
from .stock_agent import run_agent_events
from .stock_tools import stock_strategy_payload
from .settings import settings


router = APIRouter(prefix="/api", tags=["ai"])
_AGENT_STREAM_DONE = object()


class PlatformIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    provider_type: str = Field(default="OpenAI compatible", max_length=80)
    base_url: str = Field(min_length=1, max_length=255)
    api_key: str | None = Field(default=None, max_length=500)
    status: str = Field(default="enabled", max_length=24)
    remark: str | None = None


class ModelConfigIn(BaseModel):
    platform_id: int | None = None
    display_name: str = Field(min_length=1, max_length=120)
    provider_name: str = Field(default="custom", max_length=80)
    base_url: str = Field(min_length=1, max_length=255)
    api_key: str | None = Field(default=None, max_length=500)
    model_name: str = Field(min_length=1, max_length=120)
    is_active: bool = False
    status: str = Field(default="enabled", max_length=24)
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_steps: int = Field(default=6, ge=1, le=20)
    remark: str | None = None


class DigitalHumanIn(BaseModel):
    code: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    avatar_tone: str = Field(default="calm", max_length=80)
    target_roles: str = Field(default="*", max_length=255)
    description: str = Field(min_length=1, max_length=500)
    system_prompt: str = Field(min_length=1)
    quick_prompts: list[str] = []
    status: str = Field(default="enabled", max_length=24)
    sort_order: int = 0


class DataSourceConfigIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    provider_type: str = Field(default="market_data", max_length=80)
    base_url: str | None = Field(default=None, max_length=255)
    api_key: str | None = Field(default=None, max_length=500)
    status: str = Field(default="enabled", max_length=24)
    remark: str | None = None


class AgentMessageIn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class AgentChatIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    history: list[AgentMessageIn] = []
    session_id: int | None = None
    digital_human_id: int | None = None
    model_config_id: int | None = None
    chat_mode: Literal["research", "stock"] = "research"
    skills: list[str] = []


class PromptRecommendationRefreshIn(BaseModel):
    digital_human_id: int


def _sse_event(event: dict) -> str:
    event_type = str(event.get("type") or "message")
    payload = json.dumps(event, ensure_ascii=False)
    return f"event: {event_type}\ndata: {payload}\n\n"


def _masked(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def _platform_out(platform: LlmModelPlatform) -> dict:
    return {
        "id": platform.id,
        "name": platform.name,
        "provider_type": platform.provider_type,
        "base_url": platform.base_url,
        "api_key_masked": _masked(platform.api_key),
        "has_api_key": bool(platform.api_key),
        "status": platform.status,
        "remark": platform.remark,
        "updated_at": platform.updated_at.isoformat() if platform.updated_at else None,
    }


def _model_out(model: LlmModelConfig) -> dict:
    platform = model.platform
    platform_name = platform.name if platform else model.provider_name
    return {
        "id": model.id,
        "platform_id": model.platform_id,
        "platform_name": platform_name,
        "provider_name": platform_name,
        "display_name": model.display_name,
        "base_url": platform.base_url if platform else model.base_url,
        "api_key_masked": _masked((platform.api_key if platform and platform.api_key else model.api_key)),
        "has_api_key": bool((platform.api_key if platform and platform.api_key else model.api_key)),
        "model_name": model.model_name,
        "is_active": bool(model.is_active),
        "status": model.status,
        "temperature": float(model.temperature),
        "max_steps": model.max_steps,
        "remark": model.remark,
        "updated_at": model.updated_at.isoformat() if model.updated_at else None,
    }


def _connection_error(message: str, started_at: float) -> dict:
    return {"ok": False, "error": message, "latency_ms": int((time.monotonic() - started_at) * 1000)}


def _test_openai_models(base_url: str, api_key: str | None) -> dict:
    started_at = time.monotonic()
    if not base_url.strip():
        return _connection_error("Base URL 不能为空", started_at)
    if not api_key:
        return _connection_error("API Key 不能为空", started_at)
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=8.0)) as client:
            response = client.get(url, headers=headers)
    except Exception as exc:  # noqa: BLE001
        return _connection_error(f"连接失败: {str(exc)[:300]}", started_at)
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    if response.status_code >= 400:
        return {
            "ok": False,
            "status_code": response.status_code,
            "latency_ms": elapsed_ms,
            "error": response.text[:500],
        }
    try:
        payload = response.json()
    except json.JSONDecodeError:
        payload = {}
    models = []
    for item in payload.get("data") or []:
        model_id = item.get("id") if isinstance(item, dict) else None
        if model_id:
            models.append(str(model_id))
    return {
        "ok": True,
        "status_code": response.status_code,
        "latency_ms": elapsed_ms,
        "message": "平台连接正常",
        "models": models[:30],
    }


def _test_openai_chat(base_url: str, api_key: str | None, model_name: str, temperature: float = 0.2) -> dict:
    started_at = time.monotonic()
    if not base_url.strip():
        return _connection_error("Base URL 不能为空", started_at)
    if not api_key:
        return _connection_error("API Key 不能为空", started_at)
    if not model_name.strip():
        return _connection_error("模型名称不能为空", started_at)
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model_name.strip(),
        "messages": [{"role": "user", "content": "请只回复 ok，用来测试模型连通性。"}],
        "temperature": temperature,
        "max_tokens": 16,
        "stream": False,
    }
    try:
        with httpx.Client(timeout=httpx.Timeout(35.0, connect=8.0)) as client:
            response = client.post(url, json=payload, headers=headers)
    except Exception as exc:  # noqa: BLE001
        return _connection_error(f"连接失败: {str(exc)[:300]}", started_at)
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    if response.status_code >= 400:
        return {
            "ok": False,
            "status_code": response.status_code,
            "latency_ms": elapsed_ms,
            "error": response.text[:600],
        }
    try:
        data = response.json()
    except json.JSONDecodeError:
        data = {}
    content = ""
    choices = data.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        content = str(message.get("content") or "").strip()
    return {
        "ok": True,
        "status_code": response.status_code,
        "latency_ms": elapsed_ms,
        "message": "模型调用正常",
        "response_preview": content[:120],
    }


def _effective_model_connection(payload: ModelConfigIn, db: Session) -> dict:
    platform = db.get(LlmModelPlatform, payload.platform_id) if payload.platform_id else None
    return {
        "base_url": (platform.base_url if platform else payload.base_url) or "",
        "api_key": (payload.api_key or (platform.api_key if platform else None)),
        "model_name": payload.model_name,
        "temperature": payload.temperature,
    }


def _data_source_defaults() -> dict[str, dict]:
    return {
        "tushare": {
            "provider": "tushare",
            "display_name": "TuShare",
            "provider_type": "market_data",
            "base_url": "https://api.tushare.pro",
            "env_has_api_key": bool(settings.tushare_token),
            "status": "enabled",
            "remark": "A股基础信息、日线行情和后续扩展行情数据源。",
        },
        "tavily": {
            "provider": "tavily",
            "display_name": "Tavily Search",
            "provider_type": "search",
            "base_url": "https://api.tavily.com/search",
            "env_has_api_key": bool(settings.tavily_api_key),
            "status": "enabled",
            "remark": "问股消息面、新闻、公告和市场叙事搜索源。",
        },
    }


def _data_source_out(row: DataSourceConfig | None, provider: str) -> dict:
    default = _data_source_defaults()[provider]
    api_key = row.api_key if row and row.api_key else None
    env_has_api_key = bool(default["env_has_api_key"])
    return {
        "provider": provider,
        "display_name": row.display_name if row else default["display_name"],
        "provider_type": row.provider_type if row else default["provider_type"],
        "base_url": row.base_url if row and row.base_url else default["base_url"],
        "api_key_masked": _masked(api_key) if api_key else ("ENV" if env_has_api_key else None),
        "has_api_key": bool(api_key or env_has_api_key),
        "key_source": "database" if api_key else ("environment" if env_has_api_key else "missing"),
        "status": row.status if row else default["status"],
        "remark": row.remark if row and row.remark else default["remark"],
        "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
    }


def _quick_prompts(profile: DigitalHumanProfile) -> list[str]:
    try:
        value = json.loads(profile.quick_prompts_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()][:8]


def _digital_human_out(profile: DigitalHumanProfile) -> dict:
    return {
        "id": profile.id,
        "code": profile.code,
        "name": profile.name,
        "avatar_tone": profile.avatar_tone,
        "target_roles": profile.target_roles,
        "description": profile.description,
        "system_prompt": profile.system_prompt,
        "quick_prompts": _quick_prompts(profile),
        "status": profile.status,
        "sort_order": profile.sort_order,
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
    }


def _csv_match(raw: str | None, values: set[str]) -> bool:
    if not raw:
        return False
    items = {item.strip() for item in raw.split(",") if item.strip()}
    return "*" in items or bool(items & values)


def _profile_allowed(profile: DigitalHumanProfile, context: AuthContext) -> bool:
    return _csv_match(profile.target_roles, {context.user.role})


def _digital_human_context(context: AuthContext, profile: DigitalHumanProfile | None) -> str:
    parts = [
        f"当前用户: {context.user.display_name}",
        f"系统角色: {context.user.role}",
    ]
    if profile:
        parts.extend(
            [
                f"当前数字人: {profile.name}",
                f"风格: {profile.avatar_tone}",
                f"职责: {profile.description}",
                f"提示词: {profile.system_prompt}",
            ]
        )
    return "\n".join(parts)


def _title_from_question(question: str) -> str:
    return " ".join(question.strip().split())[:48] or "新的 ReAct 会话"


def _messages_for_history(db: Session, session_id: int | None) -> list[dict]:
    if session_id is None:
        return []
    rows = db.scalars(
        select(AiChatMessage)
        .where(AiChatMessage.session_id == session_id, AiChatMessage.error.is_(None))
        .order_by(AiChatMessage.id.desc())
        .limit(12)
    ).all()
    return [
        {"role": row.role, "content": row.content}
        for row in reversed(rows)
        if row.role in {"user", "assistant"} and row.content
    ]


def _store_assistant_message(session_id: int, content: str, error: str | None, event_count: int) -> None:
    if not content and not error:
        return
    with SessionLocal() as session:
        chat_session = session.get(AiChatSession, session_id)
        if chat_session is None:
            return
        session.add(
            AiChatMessage(
                session_id=session_id,
                role="assistant",
                content=content or "",
                error=error,
                event_count=event_count,
            )
        )
        chat_session.status = "error" if error else "active"
        session.commit()
        if chat_session.digital_human_id and chat_session.chat_mode == "research":
            profile = session.get(DigitalHumanProfile, chat_session.digital_human_id)
            if profile is not None:
                refresh_recommendations_for_user_profile(
                    session,
                    user_id=chat_session.user_id,
                    profile=profile,
                    chat_mode=chat_session.chat_mode,
                )


@router.get("/model-platforms")
def model_platforms(
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("model:manage")),
) -> list[dict]:
    rows = db.scalars(select(LlmModelPlatform).order_by(LlmModelPlatform.updated_at.desc(), LlmModelPlatform.id.desc())).all()
    return [_platform_out(row) for row in rows]


@router.post("/model-platforms")
def create_model_platform(
    payload: PlatformIn,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    platform = LlmModelPlatform(**payload.model_dump())
    db.add(platform)
    db.commit()
    db.refresh(platform)
    return _platform_out(platform)


@router.put("/model-platforms/{platform_id}")
def update_model_platform(
    platform_id: int,
    payload: PlatformIn,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    platform = db.get(LlmModelPlatform, platform_id)
    if platform is None:
        raise HTTPException(status_code=404, detail="模型平台不存在")
    data = payload.model_dump()
    if data.get("api_key") is None:
        data.pop("api_key", None)
    for key, value in data.items():
        setattr(platform, key, value)
    db.commit()
    db.refresh(platform)
    return _platform_out(platform)


@router.post("/model-platforms/test")
def test_model_platform_payload(
    payload: PlatformIn,
    _context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    return _test_openai_models(payload.base_url, payload.api_key)


@router.post("/model-platforms/{platform_id}/test")
def test_model_platform(
    platform_id: int,
    payload: PlatformIn | None = None,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    platform = db.get(LlmModelPlatform, platform_id)
    if platform is None:
        raise HTTPException(status_code=404, detail="模型平台不存在")
    base_url = payload.base_url if payload else platform.base_url
    api_key = payload.api_key if payload and payload.api_key else platform.api_key
    return _test_openai_models(base_url, api_key)


@router.get("/model-configs")
def model_configs(
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("agent:use")),
) -> list[dict]:
    rows = db.scalars(
        select(LlmModelConfig)
        .options(selectinload(LlmModelConfig.platform))
        .order_by(LlmModelConfig.is_active.desc(), LlmModelConfig.updated_at.desc(), LlmModelConfig.id.desc())
    ).all()
    return [_model_out(row) for row in rows]


@router.post("/model-configs")
def create_model_config(
    payload: ModelConfigIn,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    if payload.is_active:
        db.execute(update(LlmModelConfig).values(is_active=False))
    model = LlmModelConfig(**payload.model_dump())
    db.add(model)
    db.commit()
    db.refresh(model)
    return _model_out(model)


@router.put("/model-configs/{model_id}")
def update_model_config(
    model_id: int,
    payload: ModelConfigIn,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    model = db.get(LlmModelConfig, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    if payload.is_active:
        db.execute(update(LlmModelConfig).where(LlmModelConfig.id != model_id).values(is_active=False))
    data = payload.model_dump()
    if data.get("api_key") is None:
        data.pop("api_key", None)
    for key, value in data.items():
        setattr(model, key, value)
    db.commit()
    db.refresh(model)
    return _model_out(model)


@router.post("/model-configs/{model_id}/activate")
def activate_model_config(
    model_id: int,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    model = db.get(LlmModelConfig, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    db.execute(update(LlmModelConfig).where(LlmModelConfig.id != model_id).values(is_active=False))
    model.is_active = True
    model.status = "enabled"
    db.commit()
    db.refresh(model)
    return _model_out(model)


@router.post("/model-configs/test")
def test_model_config_payload(
    payload: ModelConfigIn,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    connection = _effective_model_connection(payload, db)
    return _test_openai_chat(
        connection["base_url"],
        connection["api_key"],
        connection["model_name"],
        connection["temperature"],
    )


@router.post("/model-configs/{model_id}/test")
def test_model_config(
    model_id: int,
    payload: ModelConfigIn | None = None,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    model = db.scalar(
        select(LlmModelConfig)
        .options(selectinload(LlmModelConfig.platform))
        .where(LlmModelConfig.id == model_id)
        .limit(1)
    )
    if model is None:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    if payload:
        platform = db.get(LlmModelPlatform, payload.platform_id) if payload.platform_id else None
        base_url = (platform.base_url if platform else payload.base_url) or model.base_url
        api_key = payload.api_key or model.api_key or (platform.api_key if platform else None)
        return _test_openai_chat(base_url, api_key, payload.model_name, float(payload.temperature))
    platform = model.platform
    base_url = (platform.base_url if platform else model.base_url) or ""
    api_key = model.api_key or (platform.api_key if platform else None)
    return _test_openai_chat(base_url, api_key, model.model_name, float(model.temperature))


@router.get("/data-sources")
def data_sources(
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("agent:use")),
) -> list[dict]:
    rows = {row.provider: row for row in db.scalars(select(DataSourceConfig)).all()}
    return [_data_source_out(rows.get(provider), provider) for provider in _data_source_defaults()]


@router.put("/data-sources/{provider}")
def update_data_source(
    provider: str,
    payload: DataSourceConfigIn,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    provider_key = provider.strip().lower()
    if provider_key not in _data_source_defaults():
        raise HTTPException(status_code=404, detail="数据源不存在")
    row = db.get(DataSourceConfig, provider_key)
    if row is None:
        row = DataSourceConfig(provider=provider_key)
        db.add(row)
    row.display_name = payload.display_name
    row.provider_type = payload.provider_type
    row.base_url = payload.base_url
    row.status = payload.status
    row.remark = payload.remark
    if payload.api_key is not None and payload.api_key.strip():
        row.api_key = payload.api_key.strip()
    db.commit()
    db.refresh(row)
    return _data_source_out(row, provider_key)


@router.get("/digital-humans")
def digital_humans(
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("agent:use")),
) -> list[dict]:
    rows = db.scalars(
        select(DigitalHumanProfile)
        .where(DigitalHumanProfile.status == "enabled")
        .order_by(DigitalHumanProfile.sort_order, DigitalHumanProfile.id)
    ).all()
    allowed = [row for row in rows if _profile_allowed(row, context)]
    if not allowed and rows:
        allowed = [rows[0]]
    return [_digital_human_out(row) for row in allowed]


@router.post("/digital-humans")
def create_digital_human(
    payload: DigitalHumanIn,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("digital_human:manage")),
) -> dict:
    data = payload.model_dump()
    data["quick_prompts_json"] = json.dumps(data.pop("quick_prompts"), ensure_ascii=False)
    profile = DigitalHumanProfile(**data)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return _digital_human_out(profile)


@router.put("/digital-humans/{profile_id}")
def update_digital_human(
    profile_id: int,
    payload: DigitalHumanIn,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("digital_human:manage")),
) -> dict:
    profile = db.get(DigitalHumanProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="数字人不存在")
    data = payload.model_dump()
    data["quick_prompts_json"] = json.dumps(data.pop("quick_prompts"), ensure_ascii=False)
    for key, value in data.items():
        setattr(profile, key, value)
    db.commit()
    db.refresh(profile)
    return _digital_human_out(profile)


@router.get("/agent/profiles")
def agent_profiles(
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("agent:use")),
) -> list[dict]:
    return digital_humans(db, context)


@router.get("/agent/models")
def agent_models(
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("agent:use")),
) -> list[dict]:
    return model_configs(db, _context)


@router.get("/agent/prompt-recommendations")
def prompt_recommendations(
    digital_human_id: int = Query(..., ge=1),
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    profile = db.get(DigitalHumanProfile, digital_human_id)
    if profile is None or profile.status != "enabled":
        raise HTTPException(status_code=404, detail="数字人不存在或已停用")
    if not _profile_allowed(profile, context):
        raise HTTPException(status_code=403, detail="当前账号不能使用该数字人")
    rows = active_recommendations(db, context.user.id, profile.id, limit=6)
    if not rows:
        rows = refresh_recommendations_for_user_profile(db, user_id=context.user.id, profile=profile)
    return {
        "items": [recommendation_out(row) for row in rows],
        "fallback_prompts": _quick_prompts(profile),
    }


@router.post("/agent/prompt-recommendations/refresh")
def refresh_prompt_recommendations(
    payload: PromptRecommendationRefreshIn,
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    profile = db.get(DigitalHumanProfile, payload.digital_human_id)
    if profile is None or profile.status != "enabled":
        raise HTTPException(status_code=404, detail="数字人不存在或已停用")
    if not _profile_allowed(profile, context):
        raise HTTPException(status_code=403, detail="当前账号不能使用该数字人")
    rows = refresh_recommendations_for_user_profile(db, user_id=context.user.id, profile=profile)
    return {"items": [recommendation_out(row) for row in rows], "fallback_prompts": _quick_prompts(profile)}


@router.post("/agent/prompt-recommendations/{recommendation_id}/dismiss")
def dismiss_prompt_recommendation(
    recommendation_id: int,
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    if not dismiss_recommendation(db, user_id=context.user.id, recommendation_id=recommendation_id):
        raise HTTPException(status_code=404, detail="推荐不存在或无权操作")
    return {"ok": True}


@router.get("/agent/stock/strategies")
def stock_strategies(
    _context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    return stock_strategy_payload()


@router.get("/agent/sessions")
def agent_sessions(
    chat_mode: Literal["research", "stock"] | None = Query(default=None),
    digital_human_id: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("agent:use")),
) -> list[dict]:
    query = (
        select(AiChatSession)
        .options(
            selectinload(AiChatSession.digital_human),
            selectinload(AiChatSession.model_config).selectinload(LlmModelConfig.platform),
            selectinload(AiChatSession.messages),
        )
        .where(AiChatSession.user_id == context.user.id)
    )
    if chat_mode:
        query = query.where(AiChatSession.chat_mode == chat_mode)
    if digital_human_id:
        query = query.where(AiChatSession.digital_human_id == digital_human_id)
    rows = db.scalars(
        query.order_by(AiChatSession.updated_at.desc(), AiChatSession.id.desc()).limit(50)
    ).all()
    return [
        {
            "id": row.id,
            "title": row.title,
            "chat_mode": row.chat_mode,
            "digital_human_id": row.digital_human_id,
            "digital_human_name": row.digital_human.name if row.digital_human else None,
            "model_config_id": row.model_config_id,
            "model_display_name": row.model_config.display_name if row.model_config else None,
            "status": row.status,
            "message_count": len(row.messages),
            "updated_at": row.updated_at.isoformat(),
        }
        for row in rows
    ]


@router.get("/agent/sessions/{session_id}")
def agent_session_detail(
    session_id: int,
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    row = db.scalar(
        select(AiChatSession)
        .options(
            selectinload(AiChatSession.digital_human),
            selectinload(AiChatSession.model_config).selectinload(LlmModelConfig.platform),
            selectinload(AiChatSession.messages),
        )
        .where(AiChatSession.id == session_id, AiChatSession.user_id == context.user.id)
        .limit(1)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在或无权查看")
    messages = sorted(row.messages, key=lambda item: item.id)
    return {
        "id": row.id,
        "title": row.title,
        "chat_mode": row.chat_mode,
        "digital_human_id": row.digital_human_id,
        "digital_human_name": row.digital_human.name if row.digital_human else None,
        "model_config_id": row.model_config_id,
        "model_display_name": row.model_config.display_name if row.model_config else None,
        "messages": [
            {
                "id": item.id,
                "role": item.role,
                "content": item.content,
                "error": item.error,
                "created_at": item.created_at.isoformat(),
            }
            for item in messages
            if item.role in {"user", "assistant"}
        ],
    }


@router.post("/agent/chat")
async def agent_chat(
    payload: AgentChatIn,
    request: Request,
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("agent:use")),
) -> StreamingResponse:
    profiles = db.scalars(
        select(DigitalHumanProfile)
        .where(DigitalHumanProfile.status == "enabled")
        .order_by(DigitalHumanProfile.sort_order, DigitalHumanProfile.id)
    ).all()
    allowed_profiles = [row for row in profiles if _profile_allowed(row, context)]
    profile: DigitalHumanProfile | None = None
    if payload.digital_human_id:
        profile = next((row for row in allowed_profiles if row.id == payload.digital_human_id), None)
        if profile is None:
            raise HTTPException(status_code=403, detail="当前账号不能使用该数字人")
    elif payload.chat_mode != "stock" and allowed_profiles:
        profile = allowed_profiles[0]

    model_config: LlmModelConfig | None = None
    if payload.model_config_id:
        model_config = db.scalar(
            select(LlmModelConfig)
            .where(LlmModelConfig.id == payload.model_config_id, LlmModelConfig.status != "disabled")
            .limit(1)
        )
        if model_config is None:
            raise HTTPException(status_code=404, detail="模型配置不存在或已停用")

    chat_session: AiChatSession | None = None
    if payload.session_id:
        chat_session = db.scalar(
            select(AiChatSession)
            .where(AiChatSession.id == payload.session_id, AiChatSession.user_id == context.user.id)
            .limit(1)
        )
        if chat_session is None:
            raise HTTPException(status_code=404, detail="会话不存在或无权访问")
        if chat_session.chat_mode != payload.chat_mode:
            raise HTTPException(status_code=400, detail="session mode mismatch")
        if payload.chat_mode != "stock" and profile is None and chat_session.digital_human_id:
            profile = db.get(DigitalHumanProfile, chat_session.digital_human_id)
        if model_config is None and chat_session.model_config_id:
            existing_model = db.get(LlmModelConfig, chat_session.model_config_id)
            if existing_model and existing_model.status != "disabled":
                model_config = existing_model

    if chat_session is None:
        chat_session = AiChatSession(
            user_id=context.user.id,
            digital_human_id=profile.id if profile else None,
            model_config_id=model_config.id if model_config else None,
            title=_title_from_question(payload.question),
            status="active",
            chat_mode=payload.chat_mode,
        )
        db.add(chat_session)
        db.flush()
    chat_session.chat_mode = payload.chat_mode
    if payload.chat_mode == "stock":
        chat_session.digital_human_id = None
    elif payload.digital_human_id:
        chat_session.digital_human_id = profile.id if profile else None
    if payload.model_config_id:
        chat_session.model_config_id = model_config.id if model_config else None

    history = [item.model_dump() for item in payload.history] or _messages_for_history(db, chat_session.id)
    db.add(AiChatMessage(session_id=chat_session.id, role="user", content=payload.question, event_count=0))
    db.commit()
    session_id = chat_session.id
    profile_payload = _digital_human_out(profile) if profile else None
    model_payload = _model_out(model_config) if model_config else None
    digital_human_context = _digital_human_context(context, profile)
    model_config_id = model_config.id if model_config else None

    async def event_stream():
        yield ": connected\n\n"
        yield _sse_event(
            {"type": "session", "session_id": session_id, "profile": profile_payload, "model": model_payload, "chat_mode": payload.chat_mode}
        )
        queue: asyncio.Queue[dict | object] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        content_buffer: list[str] = []
        final_answer = ""
        final_error: str | None = None
        event_count = 0

        def produce_events() -> None:
            try:
                for event in run_agent_events(
                    payload.question,
                    history,
                    digital_human_context,
                    model_config_id,
                    payload.chat_mode,
                    payload.skills,
                    profile.code if profile else None,
                ):
                    asyncio.run_coroutine_threadsafe(queue.put(event), loop).result()
            except Exception as exc:  # noqa: BLE001
                asyncio.run_coroutine_threadsafe(queue.put({"type": "error", "content": str(exc)}), loop).result()
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(_AGENT_STREAM_DONE), loop).result()

        producer = asyncio.create_task(asyncio.to_thread(produce_events))
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=8)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if event is _AGENT_STREAM_DONE:
                break
            event_count += 1
            if isinstance(event, dict):
                if event.get("type") == "content_delta":
                    content_buffer.append(str(event.get("content") or ""))
                elif event.get("type") == "content_end":
                    if event.get("role") == "answer":
                        final_answer = "".join(content_buffer).strip()
                    content_buffer.clear()
                elif event.get("type") == "error":
                    final_error = str(event.get("content") or "ReAct 响应失败")
            yield _sse_event(event)
            await asyncio.sleep(0)
        if producer.done():
            await producer
        _store_assistant_message(session_id, final_answer, final_error, event_count)
        yield _sse_event({"type": "done"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
