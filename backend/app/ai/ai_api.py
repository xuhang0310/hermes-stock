from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import case, desc, func, select, text, update
from sqlalchemy.orm import Session, selectinload

from backend.app.auth.auth import AuthContext, require_permission
from backend.app.core.database import SessionLocal, get_db
from backend.app.core.models import (
    AiChatMessage,
    AiPromptRecommendation,
    AiChatSession,
    BackgroundTask,
    DataSourceConfig,
    DigitalHumanProfile,
    Document,
    DocumentChunk,
    KnowledgeUpload,
    LlmModelConfig,
    LlmModelPlatform,
    WorkspaceDocument,
)
from backend.app.ai import memory as memory_module
from backend.app.ai import rag_ingest
from backend.app.ai.memory import retrieve_memories, schedule_memory_digestion
from backend.app.ai.session_compact import load_and_compact_history
from backend.app.ai.experience_store import retrieve_experiences
from backend.app.ai.retrieval import retrieve_chunks
from backend.app.ai.prompt_recommendations import (
    active_recommendations,
    dismiss_recommendation,
    ensure_ask_stock_profile,
    recommendation_out,
    refresh_recommendations_for_stock,
    refresh_recommendations_for_user_profile,
    schedule_recommendation_refresh,
)
from backend.app.core.applog import log_event
from backend.app.agents.stock_agent import run_agent_events
from backend.app.market.stock_tools import normalize_a_share_symbol, stock_strategy_payload
from backend.app.core.settings import settings


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
    can_write_positions: bool = False


class DataSourceConfigIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    provider_type: str = Field(default="market_data", max_length=80)
    base_url: str | None = Field(default=None, max_length=255)
    api_key: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=120)
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
    chat_mode: Literal["research", "stock", "knowledge"] = "research"
    skills: list[str] = []


class PromptRecommendationRefreshIn(BaseModel):
    digital_human_id: int


class KnowledgeSearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=0, ge=0, le=20)  # 0=用 settings.rag_top_k 默认
    symbol: str | None = Field(default=None, max_length=24)


class KnowledgeReindexIn(BaseModel):
    doc_id: int | None = Field(default=None, ge=1)  # 空=全量重建


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
        "brave": {
            "provider": "brave",
            "display_name": "Brave Search",
            "provider_type": "search",
            "base_url": "https://api.search.brave.com/res/v1/web/search",
            "env_has_api_key": bool(settings.brave_api_key),
            "status": "enabled",
            "remark": "备选联网搜索源（Brave Search API，GET + X-Subscription-Token）。填 Key 并启用后优先于 Tavily；禁用或留空则自动回退 Tavily。",
        },
        "embedding": {
            "provider": "embedding",
            "display_name": "向量嵌入",
            "provider_type": "embedding",
            "base_url": "https://api.siliconflow.cn/v1",
            "model": "BAAI/bge-m3",
            "env_has_api_key": bool(settings.embedding_api_key),
            "status": "enabled",
            "remark": "用户长期记忆的语义向量化（OpenAI 兼容 /embeddings，与聊天模型解耦）。可填 SiliconFlow、千问 DashScope 等；填了这里就以此为准，否则回退 env / 活跃 LLM。",
        },
        "rerank": {
            "provider": "rerank",
            "display_name": "记忆重排",
            "provider_type": "rerank",
            "base_url": "https://api.siliconflow.cn/v1",
            "model": "BAAI/bge-reranker-v2-m3",
            "env_has_api_key": bool(settings.embedding_api_key),
            "status": "enabled",
            "remark": "记忆检索的精排（同提供商 /rerank）。base_url/key 未填则复用嵌入配置；模型名留空则关闭重排。",
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
        "model": (row.model if row and row.model else default.get("model")),
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
        "can_write_positions": bool(profile.can_write_positions),
        "feishu_bot_enabled": bool(profile.feishu_bot_enabled),
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
    if payload.model is not None:
        row.model = payload.model.strip() or None
    db.commit()
    db.refresh(row)
    if provider_key in ("embedding", "rerank"):  # 嵌入/重排配置变更即时生效（清缓存，下次检索按新配置重建）
        try:
            from backend.app.ai import memory as _mem
            _mem._embedding_fn = None
            _mem._embedding_dim = 0
            _mem._store = None
            _mem._store_ready = None
        except Exception:
            pass
    return _data_source_out(row, provider_key)


@router.get("/digital-humans")
def digital_humans(
    scope: str | None = Query(default=None),
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("agent:use")),
) -> list[dict]:
    # 管理页传 scope=admin：持 digital_human:manage 时返回全部（含停用、跳过 target_roles），
    # 避免停用后从列表消失无法重新启用。聊天页不传 scope，走原 enabled + target_roles 过滤。
    admin_mode = scope == "admin" and (
        "digital_human:manage" in context.permissions or "all" in context.permissions
    )
    query = select(DigitalHumanProfile).order_by(DigitalHumanProfile.sort_order, DigitalHumanProfile.id)
    if not admin_mode:
        query = query.where(DigitalHumanProfile.status == "enabled")
    rows = db.scalars(query).all()
    if admin_mode:
        allowed = rows
    else:
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
    if profile.can_write_positions:
        # 新建即开写持仓也落审计（commit 后，避免记录没发生的变更）
        log_event(
            "digital_human", "INFO", "can_write_positions 新建开启",
            digital_human_id=profile.id, digital_human_code=profile.code,
            operator=_context.user.display_name, new=True,
        )
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
    old_can_write = bool(profile.can_write_positions)
    data = payload.model_dump()
    data["quick_prompts_json"] = json.dumps(data.pop("quick_prompts"), ensure_ascii=False)
    for key, value in data.items():
        setattr(profile, key, value)
    new_can_write = bool(profile.can_write_positions)
    db.commit()
    db.refresh(profile)
    if new_can_write != old_can_write:
        # 写持仓开关变化落审计（commit 后，避免记录没发生的变更）：谁、什么时候、把谁的写权限从 X 改成 Y
        log_event(
            "digital_human", "INFO", "can_write_positions 切换",
            digital_human_id=profile.id, digital_human_code=profile.code,
            operator=_context.user.display_name, old=old_can_write, new=new_can_write,
        )
    return _digital_human_out(profile)


@router.get("/digital-humans/{profile_id}/capabilities")
def digital_human_capabilities(
    profile_id: int,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    """返回某数字人的工具清单（公共/角色专属/写工具），只读，实时计算不落库。"""
    profile = db.get(DigitalHumanProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="数字人不存在")
    from backend.app.market.digital_human_tools import (
        COMMON_TOOL_NAMES,
        DEFAULT_ROLE_CODE,
        ROLE_TOOL_NAMES,
        WRITE_TOOL_NAMES,
        digital_human_tool_display_name,
        digital_human_tool_schemas,
        write_tool_schemas,
    )

    schemas_by_name = {s["function"]["name"]: s for s in digital_human_tool_schemas(profile.code)}
    write_schemas = (
        {s["function"]["name"]: s for s in write_tool_schemas()} if profile.can_write_positions else {}
    )

    def meta(name: str) -> dict:
        schema = schemas_by_name.get(name) or write_schemas.get(name) or {}
        fn = schema.get("function", {}) if schema else {}
        return {
            "name": name,
            "display_name": digital_human_tool_display_name(name),
            "description": fn.get("description", ""),
        }

    # role_tools 用实际工具集合推导（actual - 公共 - 写工具），与聊天时 _role_code fallback 一致；
    # 问股助手(ask_stock)等不在 ROLE_TOOL_NAMES 的，实际沿用 stock-pm 工具包，展示 fallback 提示。
    actual_names = [s["function"]["name"] for s in digital_human_tool_schemas(profile.code)]
    role_names = [n for n in actual_names if n not in COMMON_TOOL_NAMES and n not in WRITE_TOOL_NAMES]
    role_fallback_code = (
        DEFAULT_ROLE_CODE
        if profile.code not in ROLE_TOOL_NAMES and profile.code != DEFAULT_ROLE_CODE
        else None
    )

    # 技能 registry 驱动的工具（如 Agent 工作区），不在 legacy digital_human_tool_schemas 里，
    # 需单独从 registry 取，否则能力页看不到实际聊天时可用的工具。
    from backend.app.skills.registry import skill_registry

    def skill_meta(schema: dict) -> dict:
        name = schema["function"]["name"]
        return {
            "name": name,
            "display_name": digital_human_tool_display_name(name),
            "description": schema["function"].get("description", ""),
        }

    workspace_tools = [
        skill_meta(s)
        for s in skill_registry.get_tools_for("research", role_code=profile.code)
        if s["function"]["name"].startswith("ws_")
    ]

    return {
        "can_write_positions": bool(profile.can_write_positions),
        "common_tools": [meta(n) for n in COMMON_TOOL_NAMES],
        "role_tools": [meta(n) for n in role_names],
        "role_fallback_code": role_fallback_code,
        "write_tools": [meta(n) for n in WRITE_TOOL_NAMES] if profile.can_write_positions else [],
        "workspace_tools": workspace_tools,
    }


# --------------------------------------------------------------------------- Agent 工作区
# 借鉴 deepresearch 的"报告=聊天正文，文件按需物化"：回答先流式生成（全程可见），
# 存盘是事后一键动作——前端已有全文，直接落盘，零 token 成本。


class WorkspaceNoteIn(BaseModel):
    content: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=120)
    path: str | None = Field(default=None, max_length=300)
    session_id: int | None = None  # 生成该文档的对话，用于"关联对话"展示


def _workspace_note_path(title: str | None) -> str:
    """默认落 notes/：时间戳保证唯一，标题 slug（保留中英文数字）帮助日后检索。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^\w-]", "", (title or "").strip())[:24]
    return f"notes/{stamp}-{slug}.md" if slug else f"notes/{stamp}.md"


def _workspace_doc_key(user_id: int, path: str) -> str:
    return f"users/{user_id}/{path.lstrip('/')}"


def _register_workspace_doc(
    db: Session, *, user_id: int, path: str, title: str | None, session_id: int | None, created_by: str | None
) -> None:
    """按 path upsert 工作区文档登记行；关联对话的数字人由 session 反查。失败不阻断保存。"""
    try:
        dh_code = None
        if session_id:
            session_row = db.scalar(
                select(AiChatSession).where(AiChatSession.id == session_id, AiChatSession.user_id == user_id).limit(1)
            )
            if session_row is None:
                raise ValueError("会话不存在或无权关联工作区文档")
            if session_row and session_row.digital_human_id:
                profile = db.get(DigitalHumanProfile, session_row.digital_human_id)
                dh_code = profile.code if profile else None
        key = _workspace_doc_key(user_id, path)
        row = db.scalar(
            select(WorkspaceDocument).where(WorkspaceDocument.user_id == user_id, WorkspaceDocument.path == key).limit(1)
        )
        if row is None:
            db.add(WorkspaceDocument(
                path=key, user_id=user_id, title=title, session_id=session_id,
                digital_human_code=dh_code, created_by=created_by,
            ))
        else:  # 覆盖：更新元数据，保留 ingested_doc_id（内容变了需重新入库，交由入库端点 force）
            row.title = title or row.title
            if session_id:
                row.session_id = session_id
                row.digital_human_code = dh_code
        db.commit()
    except Exception as exc:  # noqa: BLE001 —— 登记失败不能影响文件已成功落盘
        db.rollback()
        log_event("workspace", "WARNING", "文档登记失败（已忽略）", path=path, error=str(exc)[:200])


@router.post("/workspace/save-note")
def save_workspace_note(
    payload: WorkspaceNoteIn,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    """把聊天回答一键存入 Agent 工作区，并登记（含关联对话）供"生成文档"页管理。

    复用 agent_workspace 技能的 ws_write_file——路径监狱 / 2MB 上限 / 二进制拒写 /
    审计原样生效；此处只补"谁存的 + 哪个对话生成的"元数据（技能层没有这些上下文）。
    """
    if not settings.workspace_enabled:
        raise HTTPException(status_code=400, detail="Agent 工作区未启用（STOCK_WORKSPACE_ENABLED=false）")
    from backend.app.skills.agent_workspace import workspace_user_scope, ws_write_file

    if payload.session_id:
        owned_session = db.scalar(
            select(AiChatSession).where(
                AiChatSession.id == payload.session_id,
                AiChatSession.user_id == _context.user.id,
                AiChatSession.status != "deleted",
            ).limit(1)
        )
        if owned_session is None:
            raise HTTPException(status_code=404, detail="会话不存在或无权关联工作区文档")

    path = (payload.path or "").strip() or _workspace_note_path(payload.title)
    with workspace_user_scope(_context.user.id):
        result = ws_write_file(path, payload.content)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    _register_workspace_doc(
        db, user_id=_context.user.id, path=result["path"], title=payload.title,
        session_id=payload.session_id, created_by=_context.user.display_name,
    )
    log_event(
        "workspace", "INFO", "回答存入工作区",
        operator=_context.user.display_name, path=result.get("path"), bytes=result.get("bytes"),
    )
    return result


_WORKSPACE_TEXT_EXTS = {".md", ".txt", ".csv", ".json", ".py"}


@router.get("/workspace/documents")
def list_workspace_documents(
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("agent:use")),
) -> list[dict]:
    """列出工作区里所有生成的文档：文件系统为存在性权威，LEFT JOIN 登记表补关联对话与入库状态。"""
    if not settings.workspace_enabled:
        return []
    from backend.app.skills.agent_workspace import _ensure_root, workspace_user_scope

    with workspace_user_scope(_context.user.id):
        root = _ensure_root()
    registry = {
        row.path: row
        for row in db.scalars(select(WorkspaceDocument).where(WorkspaceDocument.user_id == _context.user.id)).all()
    }
    sessions = {
        s.id: s for s in db.scalars(
            select(AiChatSession).where(AiChatSession.id.in_([r.session_id for r in registry.values() if r.session_id]))
        ).all()
    } if registry else {}
    docs: list[dict] = []
    for file in root.rglob("*"):
        if not file.is_file() or file.suffix.lower() not in _WORKSPACE_TEXT_EXTS:
            continue
        rel = file.resolve().relative_to(root).as_posix()
        stat = file.stat()
        meta = registry.get(_workspace_doc_key(_context.user.id, rel))
        session = sessions.get(meta.session_id) if meta and meta.session_id else None
        docs.append({
            "path": rel,
            "name": file.name,
            "bytes": stat.st_size,
            "mtime": int(stat.st_mtime),
            "title": (meta.title if meta else None) or file.stem,
            "created_by": meta.created_by if meta else None,
            "digital_human_code": meta.digital_human_code if meta else None,
            "session": {"id": session.id, "title": session.title, "chat_mode": session.chat_mode} if session else None,
            "ingested": bool(meta and meta.ingested_doc_id),
            "ingested_doc_id": meta.ingested_doc_id if meta else None,
        })
    docs.sort(key=lambda d: d["mtime"], reverse=True)
    return docs


@router.get("/workspace/documents/download")
def download_workspace_document(
    path: str = Query(..., max_length=300),
    _context: AuthContext = Depends(require_permission("agent:use")),
) -> FileResponse:
    """下载工作区文档（路径经监狱校验，只能取工作区内文件）。"""
    from backend.app.skills.agent_workspace import WorkspaceError, _resolve_in_jail, workspace_user_scope

    try:
        with workspace_user_scope(_context.user.id):
            target = _resolve_in_jail(path)
    except WorkspaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文档不存在")
    return FileResponse(str(target), filename=target.name, media_type="application/octet-stream")


class WorkspaceIngestIn(BaseModel):
    path: str = Field(min_length=1, max_length=300)


@router.post("/workspace/documents/ingest")
def ingest_workspace_document(
    payload: WorkspaceIngestIn,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    """把工作区文档加入知识库：建 Document（复用上传同款）→ 切块嵌入索引 → 回填 ingested_doc_id。"""
    from backend.app.skills.agent_workspace import WorkspaceError, _resolve_in_jail, workspace_user_scope

    try:
        with workspace_user_scope(_context.user.id):
            target = _resolve_in_jail(payload.path)
    except WorkspaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文档不存在")
    text_content = target.read_text(encoding="utf-8", errors="replace")
    if not text_content.strip():
        raise HTTPException(status_code=400, detail="文档内容为空，无法入库")

    content_hash = hashlib.sha256(text_content.encode("utf-8")).hexdigest()[:64]
    existing = db.scalar(select(Document).where(Document.content_hash == content_hash).limit(1))
    if existing:
        doc = existing
    else:
        doc = Document(
            doc_type="workspace",
            title=target.stem,
            content=text_content,
            summary=text_content[:500],
            source="workspace",
            trust_level="trusted",
            is_external_untrusted=False,
            content_hash=content_hash,
        )
        db.add(doc)
        db.flush()
    doc_id = doc.id
    # 登记回填（没有登记行则补建一条，标记已入库）
    key = _workspace_doc_key(_context.user.id, payload.path)
    row = db.scalar(
        select(WorkspaceDocument).where(WorkspaceDocument.user_id == _context.user.id, WorkspaceDocument.path == key).limit(1)
    )
    if row is None:
        row = WorkspaceDocument(
            path=key, user_id=_context.user.id, title=target.stem, created_by=_context.user.display_name
        )
        db.add(row)
    row.ingested_doc_id = doc_id
    db.commit()

    try:
        ok, failed = rag_ingest.ingest_document(doc_id, force=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"入库切块/索引失败: {str(exc)[:200]}")
    log_event(
        "workspace", "INFO", "文档加入知识库",
        operator=_context.user.display_name, path=payload.path, doc_id=doc_id, chunks=ok, failed=failed,
    )
    return {"doc_id": doc_id, "chunks": ok, "failed": failed, "path": payload.path}


# --------------------------------------------------------------------------- RAG 知识库（P1）
# 端点只做 HTTP 编排：摄取在 rag_ingest、检索在 retrieval（单一职责）。


@router.get("/knowledge/stats")
def knowledge_stats(
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("system:view")),
) -> dict:
    total_docs = db.scalar(select(func.count()).select_from(Document)) or 0
    total_chunks = db.scalar(select(func.count()).select_from(DocumentChunk)) or 0
    searchable_chunks = db.scalar(
        select(func.count()).select_from(DocumentChunk).where(DocumentChunk.status == "indexed")
    ) or 0
    indexed_docs = db.scalar(select(func.count(func.distinct(DocumentChunk.doc_id)))) or 0
    vector_status = rag_ingest.vector_store_status()
    vector_count = int(vector_status["count"])
    by_type = {
        str(doc_type): int(count)
        for doc_type, count in db.execute(select(Document.doc_type, func.count()).group_by(Document.doc_type))
    }
    return {
        "documents": total_docs,
        "chunks": total_chunks,
        "indexed_docs": indexed_docs,
        "indexed_pct": round(indexed_docs / total_docs * 100, 1) if total_docs else 0.0,
        "searchable_chunks": searchable_chunks,
        "vector_count": vector_count,
        "vector_pct": round(vector_count / searchable_chunks * 100, 1) if searchable_chunks else 0.0,
        "vector_healthy": bool(vector_status["healthy"]),
        "vector_error": str(vector_status["error"]),
        "backend": settings.vector_backend,
        "collection": settings.rag_milvus_collection,
        "dim": memory_module._embedding_dim,
        "by_type": by_type,
        "ingest": rag_ingest.get_progress(),  # 摄取进度：前端进度条与完成判定的唯一数据源
    }


@router.get("/knowledge/documents")
def knowledge_documents(
    doc_type: str | None = Query(default=None, max_length=32),
    trust_level: str | None = Query(default=None, max_length=24),
    keyword: str | None = Query(default=None, max_length=120),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("system:view")),
) -> dict:
    chunk_agg = (
        select(
            DocumentChunk.doc_id,
            func.count().label("chunk_count"),
            func.sum(case((DocumentChunk.status == "indexed", 1), else_=0)).label("indexed_chunks"),
        )
        .group_by(DocumentChunk.doc_id)
        .subquery()
    )
    query = select(
        Document,
        func.coalesce(chunk_agg.c.chunk_count, 0),
        func.coalesce(chunk_agg.c.indexed_chunks, 0),
    ).outerjoin(chunk_agg, chunk_agg.c.doc_id == Document.id)
    if doc_type:
        query = query.where(Document.doc_type == doc_type)
    if trust_level:
        query = query.where(Document.trust_level == trust_level)
    if keyword and keyword.strip():
        query = query.where(Document.title.like(f"%{keyword.strip()}%"))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.execute(
        query.order_by(desc(Document.id)).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": doc.id,
                "title": doc.title,
                "doc_type": doc.doc_type,
                "source": doc.source,
                "source_url": doc.source_url,
                "trust_level": doc.trust_level,
                "published_at": doc.published_at.isoformat() if doc.published_at else None,
                "fetched_at": doc.fetched_at.isoformat() if doc.fetched_at else None,
                "chunk_count": int(count),
                "indexed": bool(indexed_chunks),
                # indexed=有可检索切块；disabled=有切块但全部被禁用；pending=尚未索引
                "status": "indexed" if indexed_chunks else ("disabled" if count else "pending"),
            }
            for doc, count, indexed_chunks in rows
        ],
    }


@router.get("/knowledge/documents/{doc_id}/chunks")
def knowledge_document_chunks(
    doc_id: int,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("system:view")),
) -> list[dict]:
    rows = db.scalars(
        select(DocumentChunk).where(DocumentChunk.doc_id == doc_id).order_by(DocumentChunk.chunk_index)
    ).all()
    return [
        {
            "id": chunk.id,
            "chunk_index": chunk.chunk_index,
            "content": chunk.content[:300] + ("…" if len(chunk.content) > 300 else ""),
            "token_count": chunk.token_count,
            "status": chunk.status,
            "hit_count": chunk.hit_count,
        }
        for chunk in rows
    ]


@router.post("/knowledge/documents/{doc_id}/disable")
def knowledge_document_disable(
    doc_id: int,
    _context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    result = rag_ingest.disable_document(doc_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


_reindex_tasks: set[asyncio.Task] = set()
_reindex_running = False


def _reindex_done(_task: asyncio.Task) -> None:
    global _reindex_running
    _reindex_running = False


@router.post("/knowledge/reindex-legacy", include_in_schema=False)
async def knowledge_reindex(
    payload: KnowledgeReindexIn,
    _context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    # 后台执行立即返回（全量≈数百次嵌入调用要跑几分钟，同步等会撞网关超时）；进度轮询 /knowledge/stats
    global _reindex_running
    if _reindex_running:
        raise HTTPException(status_code=409, detail="已有重建任务在跑，请等完成再发起")
    if payload.doc_id:
        task = asyncio.create_task(asyncio.to_thread(rag_ingest.ingest_document, payload.doc_id, force=True))
    else:
        task = asyncio.create_task(asyncio.to_thread(rag_ingest.run_ingest, force=True))
    _reindex_tasks.add(task)  # 持引用防 GC；done 回调自动清理
    task.add_done_callback(_reindex_tasks.discard)
    task.add_done_callback(_reindex_done)
    _reindex_running = True
    return {"started": True, "scope": payload.doc_id or "all"}


def _run_durable_reindex(task_id: int) -> None:
    with SessionLocal() as db:
        task = db.get(BackgroundTask, task_id)
        if task is None or task.status == "completed":
            return
        task.status = "running"
        task.attempts += 1
        task.error = None
        payload = json.loads(task.payload_json or "{}")
        db.commit()
    try:
        doc_id = payload.get("doc_id")
        if doc_id:
            rag_ingest.ingest_document(int(doc_id), force=True)
        else:
            rag_ingest.run_ingest(force=True)
    except Exception as exc:  # noqa: BLE001
        with SessionLocal() as db:
            task = db.get(BackgroundTask, task_id)
            if task:
                task.status = "failed"
                task.error = str(exc)[:500]
                db.commit()
        return
    with SessionLocal() as db:
        task = db.get(BackgroundTask, task_id)
        if task:
            task.status = "completed"
            db.commit()


def _schedule_durable_reindex(task_id: int) -> None:
    task = asyncio.create_task(asyncio.to_thread(_run_durable_reindex, task_id))
    _reindex_tasks.add(task)
    task.add_done_callback(_reindex_tasks.discard)


async def resume_background_tasks() -> None:
    with SessionLocal() as db:
        rows = db.scalars(
            select(BackgroundTask).where(
                BackgroundTask.task_type == "knowledge_reindex",
                BackgroundTask.status.in_(("queued", "running")),
            )
        ).all()
        for row in rows:
            row.status = "queued"
        db.commit()
        task_ids = [row.id for row in rows]
    for task_id in task_ids:
        _schedule_durable_reindex(task_id)


@router.post("/knowledge/reindex")
async def knowledge_reindex_durable(
    payload: KnowledgeReindexIn,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    running = db.scalar(
        select(BackgroundTask.id).where(
            BackgroundTask.task_type == "knowledge_reindex",
            BackgroundTask.status.in_(("queued", "running")),
        ).limit(1)
    )
    if running:
        raise HTTPException(status_code=409, detail="已有重建任务在运行，请等完成再发起")
    task_row = BackgroundTask(task_type="knowledge_reindex", payload_json=json.dumps({"doc_id": payload.doc_id}))
    db.add(task_row)
    db.commit()
    _schedule_durable_reindex(task_row.id)
    return {"started": True, "task_id": task_row.id, "scope": payload.doc_id or "all"}


_upload_tasks: set[asyncio.Task] = set()


def _parse_upload(content: bytes, ext: str) -> str:
    """解析上传文件为纯文本，全链路支持表格提取。

    PDF：pdfplumber 优先（表格/布局远优于 pypdf），不可用时降级 pypdf
    DOCX：段落 + 表格（python-docx）
    PPTX：文本框 + 表格（python-pptx）
    txt/md：多编码兜底直读
    """
    if ext == '.pdf':
        # 优先 pdfplumber：表格提取、布局保留远优于 pypdf，财务文档必备
        try:
            from pdfplumber import open as pdf_open
            from io import BytesIO

            parts: list[str] = []
            with pdf_open(BytesIO(content)) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        parts.append(page_text)
                    tables = page.extract_tables()
                    for table in tables:
                        for row in table:
                            parts.append(' | '.join(c or '' for c in row))
            text = '\n'.join(parts)
            if text.strip():
                return text
        except ImportError:
            pass
        # 降级 pypdf（纯文本，无表格）
        try:
            from pypdf import PdfReader
            from io import BytesIO

            return '\n'.join((page.extract_text() or '') for page in PdfReader(BytesIO(content)).pages)
        except ImportError:
            raise RuntimeError("PDF 解析需要 pypdf 或 pdfplumber：请在服务器 venv 执行 pip install pypdf pdfplumber")
    if ext == '.docx':
        try:
            import docx
            from io import BytesIO

            doc = docx.Document(BytesIO(content))
            parts: list[str] = []
            for p in doc.paragraphs:
                if p.text.strip():
                    parts.append(p.text)
            for table in doc.tables:
                for row in table.rows:
                    parts.append(' | '.join(cell.text for cell in row.cells))
            return '\n'.join(parts)
        except ImportError:
            raise RuntimeError("Word 解析需要 python-docx：请在服务器 venv 执行 pip install python-docx")
    if ext == '.pptx':
        try:
            from pptx import Presentation
            from io import BytesIO

            prs = Presentation(BytesIO(content))
            parts: list[str] = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        for para in shape.text_frame.paragraphs:
                            if para.text.strip():
                                parts.append(para.text)
                    if shape.has_table:
                        for row in shape.table.rows:
                            parts.append(' | '.join(cell.text for cell in row.cells))
            return '\n'.join(parts)
        except ImportError:
            raise RuntimeError("PPTX 解析需要 python-pptx：请在服务器 venv 执行 pip install python-pptx")
    for enc in ('utf-8', 'gbk', 'gb2312', 'latin-1'):
        try:
            return content.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode('utf-8', errors='replace')


def _ingest_upload(upload_id: int, doc_id: int) -> None:
    """后台摄取上传文档：切块→嵌入→索引，更新 KnowledgeUpload 状态。"""
    try:
        ok, failed = rag_ingest.ingest_document(doc_id, force=True)
        with SessionLocal() as db:
            upload = db.get(KnowledgeUpload, upload_id)
            if upload:
                if ok:
                    upload.status = 'indexed'
                    upload.chunk_count = ok
                    upload.error = None
                elif failed:
                    upload.status = 'failed'
                    upload.chunk_count = 0
                    upload.error = f'{failed} 块嵌入/索引失败'
                else:
                    upload.status = 'empty'
                    upload.chunk_count = 0
                    upload.error = '正文过短或为空，未产生切块'
                db.commit()
    except Exception as exc:  # noqa: BLE001
        with SessionLocal() as db:
            upload = db.get(KnowledgeUpload, upload_id)
            if upload:
                upload.status = 'failed'
                upload.error = str(exc)[:500]
                db.commit()


def _schedule_upload_ingest(upload_id: int, doc_id: int) -> None:
    task = asyncio.create_task(asyncio.to_thread(_ingest_upload, upload_id, doc_id))
    _upload_tasks.add(task)
    task.add_done_callback(_upload_tasks.discard)


async def resume_pending_uploads() -> None:
    """Resume committed uploads left in parsing state by a process restart."""
    with SessionLocal() as db:
        rows = db.scalars(
            select(KnowledgeUpload).where(KnowledgeUpload.status == "parsing", KnowledgeUpload.doc_id.isnot(None))
        ).all()
        pending = [(row.id, row.doc_id) for row in rows if row.doc_id is not None]
    for upload_id, doc_id in pending:
        _schedule_upload_ingest(upload_id, doc_id)


@router.post("/knowledge/upload")
async def knowledge_upload(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    allowed_ext = {'.pdf', '.docx', '.pptx', '.txt', '.md'}
    ext = Path(file.filename or '').suffix.lower()
    if ext not in allowed_ext:
        raise HTTPException(400, f"不支持的文件类型: {ext}，仅支持 PDF/Word/PPTX/txt/md")
    content = await file.read()
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(400, "文件大小超过 20MB")
    content_hash = hashlib.sha256(content).hexdigest()[:64]
    if db.scalar(select(Document).where(Document.content_hash == content_hash).limit(1)):
        raise HTTPException(409, "该文件已上传过（内容哈希相同），无需重复上传")
    try:
        text = _parse_upload(content, ext)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    if not text or not text.strip():
        raise HTTPException(400, "文件内容为空或解析失败")
    doc = Document(
        doc_type='upload',
        title=file.filename or '未命名上传',
        content=text,
        summary=text[:500],
        source='admin',
        trust_level='trusted',
        is_external_untrusted=False,
        content_hash=content_hash,
    )
    db.add(doc)
    db.flush()
    upload = KnowledgeUpload(
        filename=file.filename or '',
        mime_type=file.content_type or f'application/{ext.lstrip(".")}',
        size=len(content),
        doc_id=doc.id,
        status='parsing',
        uploaded_by=_context.user.display_name,
    )
    db.add(upload)
    db.flush()
    db.commit()
    _schedule_upload_ingest(upload.id, doc.id)
    return {"upload_id": upload.id, "doc_id": doc.id}


@router.get("/knowledge/uploads/{upload_id}")
def knowledge_upload_status(
    upload_id: int,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("system:view")),
) -> dict:
    upload = db.get(KnowledgeUpload, upload_id)
    if upload is None:
        raise HTTPException(404, "上传批次不存在")
    return {
        "id": upload.id,
        "filename": upload.filename,
        "status": upload.status,
        "chunk_count": upload.chunk_count,
        "error": upload.error,
    }


@router.post("/knowledge/search")
def knowledge_search(
    payload: KnowledgeSearchIn,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("system:view")),
) -> dict:
    symbol = normalize_a_share_symbol(payload.symbol) if payload.symbol else None
    hits = retrieve_chunks(db, payload.query.strip(), top_k=payload.top_k or None, symbol=symbol)
    return {"query": payload.query.strip(), "hits": hits}


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


@router.get("/agent/stock/prompt-recommendations")
def stock_prompt_recommendations(
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    """问股（stock 模式）推荐 prompt：复用问股虚拟 profile，会话历史按 chat_mode=stock 过滤。"""
    profile = ensure_ask_stock_profile(db)
    rows = active_recommendations(db, context.user.id, profile.id, limit=6)
    if not rows:
        rows = refresh_recommendations_for_stock(db, user_id=context.user.id)
    return {
        "items": [recommendation_out(row) for row in rows],
        "fallback_prompts": _quick_prompts(profile),
    }


@router.post("/agent/stock/prompt-recommendations/refresh")
def refresh_stock_prompt_recommendations(
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    profile = ensure_ask_stock_profile(db)
    rows = refresh_recommendations_for_stock(db, user_id=context.user.id)
    return {"items": [recommendation_out(row) for row in rows], "fallback_prompts": _quick_prompts(profile)}


@router.get("/agent/stock/strategies")
def stock_strategies(
    _context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    return stock_strategy_payload()


@router.get("/agent/sessions")
def agent_sessions(
    chat_mode: Literal["research", "stock", "knowledge"] | None = Query(default=None),
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
        .where(AiChatSession.user_id == context.user.id, AiChatSession.status != "deleted")
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
        .where(AiChatSession.id == session_id, AiChatSession.user_id == context.user.id, AiChatSession.status != "deleted")
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


@router.delete("/agent/sessions/{session_id}")
def agent_session_delete(
    session_id: int,
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    row = db.scalar(
        select(AiChatSession)
        .where(AiChatSession.id == session_id, AiChatSession.user_id == context.user.id)
        .limit(1)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在或无权操作")
    row.status = "deleted"  # 逻辑删除：仅从历史列表隐藏，消息与会话记录仍保留在库中
    db.commit()
    return {"ok": True, "id": session_id, "status": row.status}


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
    chat_session: AiChatSession | None = None
    if payload.session_id:
        chat_session = db.scalar(
            select(AiChatSession)
            .where(
                AiChatSession.id == payload.session_id,
                AiChatSession.user_id == context.user.id,
                AiChatSession.status != "deleted",
            )
            .limit(1)
        )
        if chat_session is None:
            raise HTTPException(status_code=404, detail="会话不存在或无权访问")
        if chat_session.chat_mode != payload.chat_mode:
            raise HTTPException(status_code=400, detail="session mode mismatch")

    profile: DigitalHumanProfile | None = None
    if payload.chat_mode not in ("stock", "knowledge"):
        requested_profile_id = payload.digital_human_id or (chat_session.digital_human_id if chat_session else None)
        if requested_profile_id:
            profile = next((row for row in allowed_profiles if row.id == requested_profile_id), None)
            if profile is None:
                raise HTTPException(status_code=403, detail="当前账号不能使用该数字人")
        elif allowed_profiles:
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
    if chat_session is not None:
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
    if payload.chat_mode in ("stock", "knowledge"):
        chat_session.digital_human_id = None
    elif payload.digital_human_id:
        chat_session.digital_human_id = profile.id if profile else None
    if payload.model_config_id:
        chat_session.model_config_id = model_config.id if model_config else None

    # A persisted session is the source of truth. Client-provided history is only
    # used while creating a new session, preventing stale tabs from rewriting context.
    # L1 会话记忆：读历史 + 压缩判断（替代原 limit(12) 硬截断，旧消息压成摘要保主线）
    if payload.session_id:
        history, l1_summary = load_and_compact_history(db, chat_session.id)
    else:
        history = [item.model_dump() for item in payload.history]
        l1_summary = ""
    db.add(AiChatMessage(session_id=chat_session.id, role="user", content=payload.question, event_count=0))
    db.commit()
    session_id = chat_session.id
    profile_payload = _digital_human_out(profile) if profile else None
    model_payload = _model_out(model_config) if model_config else None
    digital_human_context = _digital_human_context(context, profile)
    model_config_id = model_config.id if model_config else None
    # 检索用户长期记忆，注入到 system prompt
    memory_context = retrieve_memories(
        db, user_id=context.user.id, query=payload.question,
        digital_human_id=profile.id if profile else None,
    )
    # L1 压缩摘要拼入 system prompt（复用 memory_context 注入通道，不改 history 的 role 结构）
    if l1_summary:
        memory_context = f"{memory_context}\n\n{l1_summary}" if memory_context else l1_summary
    # L2 经验记忆：检索跨用户共享的复盘经验，注入 system prompt（带样本量，统计强度低者审慎）
    experience_context = retrieve_experiences(db, query=payload.question)
    if experience_context:
        memory_context = f"{memory_context}\n\n{experience_context}" if memory_context else experience_context

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
        cancel_event = threading.Event()
        # 三重门之一/二：数字人开 can_write_positions 且用户有 data:write 权限才注入写工具
        can_write_positions = bool(
            profile and profile.can_write_positions
            and payload.chat_mode == "research"
            and ("data:write" in context.permissions or "all" in context.permissions)
        )
        default_account_id: int | None = None
        if can_write_positions:
            default_account_id = db.scalar(
                text("SELECT id FROM position_accounts WHERE is_default = 1 ORDER BY id LIMIT 1")
            )

        def produce_events() -> None:
            started = time.monotonic()
            log_event(
                "agent", "INFO", "对话开始",
                to_db=False, chat_mode=payload.chat_mode,
                digital_human=(profile.code if profile else None),
                question=payload.question[:80],
            )
            try:
                for event in run_agent_events(
                    payload.question,
                    history,
                    digital_human_context,
                    model_config_id,
                    payload.chat_mode,
                    payload.skills,
                    profile.code if profile else None,
                    memory_context,
                    user_id=context.user.id,
                    user_label=context.user.display_name,
                    can_write_positions=can_write_positions,
                    default_account_id=default_account_id,
                    cancel_event=cancel_event,
                ):
                    asyncio.run_coroutine_threadsafe(queue.put(event), loop).result()
            except Exception as exc:  # noqa: BLE001
                log_event(
                    "agent", "ERROR", "对话失败",
                    chat_mode=payload.chat_mode,
                    digital_human=(profile.code if profile else None),
                    error=str(exc)[:300],
                )
                asyncio.run_coroutine_threadsafe(queue.put({"type": "error", "content": str(exc)}), loop).result()
            else:
                log_event(
                    "agent", "INFO", "对话完成",
                    to_db=False, chat_mode=payload.chat_mode, ms=int((time.monotonic() - started) * 1000),
                )
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(_AGENT_STREAM_DONE), loop).result()

        producer = asyncio.create_task(asyncio.to_thread(produce_events))
        disconnected = False
        while True:
            if await request.is_disconnected():
                disconnected = True
                cancel_event.set()
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=8)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if event is _AGENT_STREAM_DONE:
                break
            if not (isinstance(event, dict) and event.get("type") == "tool_progress"):
                event_count += 1  # 进度事件是高频瞬态流，不计入持久化的事件数
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
        if disconnected:
            # The worker checks the same flag before every new model/tool step.
            # Do not persist a partial assistant message or derive memories from it.
            return
        await producer
        _store_assistant_message(session_id, final_answer, final_error, event_count)
        # 推荐问题会再调用一次 LLM，必须脱离终止链路；否则答案显示完后 done 还会延迟十几秒。
        if profile is not None and payload.chat_mode == "research":
            schedule_recommendation_refresh(
                user_id=context.user.id,
                profile_id=profile.id,
                chat_mode=payload.chat_mode,
            )
        # 记忆消化同样后台执行，不阻塞 SSE 响应。
        asyncio.create_task(schedule_memory_digestion(session_id, context.user.id, profile.id if profile else None))
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
