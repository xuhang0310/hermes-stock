"""数字人 ↔ 飞书机器人（自建应用）绑定管理 API。App Secret 只存密文、只回掩码。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import AuthContext, require_permission
from .database import get_db
from .feishu import decrypt_value, encrypt_value, is_cipher_invalid
from .feishu_bot import bot_runtime, fetch_bot_info, fetch_tenant_token, restart_bot
from .models import DigitalHumanProfile
from .settings import settings


router = APIRouter(prefix="/api", tags=["feishu-bot"])


class FeishuBotIn(BaseModel):
    app_id: str | None = Field(default=None, max_length=80)
    app_secret: str | None = Field(default=None, max_length=200)
    enabled: bool = True


class FeishuBotTestIn(BaseModel):
    app_id: str | None = Field(default=None, max_length=80)
    app_secret: str | None = Field(default=None, max_length=200)


def _mask_app_id(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 10:
        return value[:4] + "****"
    return f"{value[:8]}****{value[-3:]}"


def _binding_out(profile: DigitalHumanProfile) -> dict:
    secret_plain = decrypt_value(profile.feishu_app_secret_cipher)
    return {
        "profile_id": profile.id,
        "code": profile.code,
        "name": profile.name,
        "app_id_masked": _mask_app_id(profile.feishu_app_id),
        "has_secret": bool(secret_plain),
        "cipher_invalid": is_cipher_invalid(profile.feishu_app_secret_cipher),
        "enabled": bool(profile.feishu_bot_enabled),
        "runtime": bot_runtime(profile.id),
        "feature_enabled": settings.feishu_bot_enabled,
    }


def _get_profile(db: Session, profile_id: int) -> DigitalHumanProfile:
    profile = db.get(DigitalHumanProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="数字人不存在")
    return profile


@router.get("/feishu-bots")
def feishu_bots(
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("digital_human:manage")),
) -> list[dict]:
    rows = db.scalars(
        select(DigitalHumanProfile).order_by(DigitalHumanProfile.sort_order, DigitalHumanProfile.id)
    ).all()
    return [_binding_out(row) for row in rows]


@router.put("/feishu-bots/{profile_id}")
def save_feishu_bot(
    profile_id: int,
    payload: FeishuBotIn,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("digital_human:manage")),
) -> dict:
    profile = _get_profile(db, profile_id)
    app_id = (payload.app_id or "").strip() or (profile.feishu_app_id or "")
    if not app_id:
        raise HTTPException(status_code=400, detail="请填写 App ID")
    if not app_id.startswith("cli_"):
        raise HTTPException(status_code=400, detail="App ID 格式不正确（自建应用以 cli_ 开头）")
    duplicate = db.scalar(
        select(DigitalHumanProfile)
        .where(DigitalHumanProfile.feishu_app_id == app_id, DigitalHumanProfile.id != profile_id)
        .limit(1)
    )
    if duplicate is not None:
        raise HTTPException(status_code=400, detail=f"该飞书应用已绑定数字人「{duplicate.name}」，一个应用只能绑定一个数字人")
    profile.feishu_app_id = app_id
    if payload.app_secret is not None and payload.app_secret.strip():
        profile.feishu_app_secret_cipher = encrypt_value(payload.app_secret)
    if not decrypt_value(profile.feishu_app_secret_cipher):
        raise HTTPException(status_code=400, detail="首次绑定必须填写 App Secret")
    profile.feishu_bot_enabled = payload.enabled
    db.commit()
    db.refresh(profile)
    error = restart_bot(profile_id)
    result = _binding_out(profile)
    if error and payload.enabled:
        result["runtime"]["error"] = error
    return result


@router.post("/feishu-bots/{profile_id}/test")
def test_feishu_bot(
    profile_id: int,
    payload: FeishuBotTestIn,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("digital_human:manage")),
) -> dict:
    profile = _get_profile(db, profile_id)
    app_id = (payload.app_id or "").strip() or (profile.feishu_app_id or "")
    app_secret = (payload.app_secret or "").strip() or decrypt_value(profile.feishu_app_secret_cipher) or ""
    if not app_id or not app_secret:
        raise HTTPException(status_code=400, detail="请先填写 App ID 与 App Secret，或先保存绑定")
    started = time.monotonic()
    token, _expire, error = fetch_tenant_token(app_id, app_secret)
    latency_ms = int((time.monotonic() - started) * 1000)
    if token is None:
        return {"ok": False, "error": error, "latency_ms": latency_ms}
    info = fetch_bot_info(token)
    if info.get("error"):
        return {"ok": False, "error": f"凭据有效，但机器人信息获取失败：{info['error']}（请确认已开启机器人能力）", "latency_ms": latency_ms}
    return {"ok": True, "message": f"凭据有效 · 机器人「{info.get('name') or '未命名'}」", "latency_ms": latency_ms}


@router.post("/feishu-bots/{profile_id}/toggle")
def toggle_feishu_bot(
    profile_id: int,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("digital_human:manage")),
) -> dict:
    profile = _get_profile(db, profile_id)
    if not profile.feishu_app_id:
        raise HTTPException(status_code=400, detail="该数字人尚未绑定飞书应用")
    profile.feishu_bot_enabled = not profile.feishu_bot_enabled
    db.commit()
    db.refresh(profile)
    error = restart_bot(profile_id)
    result = _binding_out(profile)
    if error and profile.feishu_bot_enabled:
        result["runtime"]["error"] = error
    return result


@router.delete("/feishu-bots/{profile_id}")
def unbind_feishu_bot(
    profile_id: int,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("digital_human:manage")),
) -> dict:
    profile = _get_profile(db, profile_id)
    profile.feishu_app_id = None
    profile.feishu_app_secret_cipher = None
    profile.feishu_bot_enabled = False
    db.commit()
    restart_bot(profile_id)
    return {"ok": True}
