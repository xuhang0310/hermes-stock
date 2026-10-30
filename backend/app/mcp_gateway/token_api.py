"""Manage external-agent credentials with the user's existing web login."""
from __future__ import annotations

import secrets
from datetime import timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.auth.auth import AuthContext, get_current_user
from backend.app.core.database import get_db
from backend.app.core.models import McpAccessToken
from backend.app.mcp_gateway.auth import READ_SCOPES, TOKEN_PREFIX, allowed_scopes, token_digest, utc_now


router = APIRouter(prefix="/api/mcp-tokens", tags=["mcp-tokens"])
McpScope = Literal["market:read", "portfolio:read", "portfolio:write", "schedules:read", "schedules:write"]


class CreateMcpTokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    scopes: list[McpScope] | None = Field(default=None, min_length=1, max_length=5)
    expires_in_days: int = Field(default=90, ge=1, le=365, strict=True)

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("凭据名称不能为空")
        return value


def _out(row: McpAccessToken) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "token_prefix": row.token_prefix,
        "scopes": row.scopes.split(","),
        "created_at": row.created_at.replace(tzinfo=timezone.utc).isoformat(),
        "expires_at": row.expires_at.replace(tzinfo=timezone.utc).isoformat(),
        "revoked_at": row.revoked_at.replace(tzinfo=timezone.utc).isoformat() if row.revoked_at else None,
        "is_expired": row.expires_at <= utc_now(),
    }


@router.get("")
def list_tokens(
    context: Annotated[AuthContext, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    rows = db.scalars(
        select(McpAccessToken).where(McpAccessToken.user_id == context.user.id)
        .order_by(McpAccessToken.id.desc())
    ).all()
    return {
        "tokens": [_out(row) for row in rows],
        "available_scopes": sorted(allowed_scopes(context.permissions)),
        "endpoint": "/api/mcp/",
    }


@router.post("", status_code=201)
def create_token(
    payload: CreateMcpTokenIn,
    response: Response,
    context: Annotated[AuthContext, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    available = allowed_scopes(context.permissions)
    scopes = frozenset(payload.scopes) if payload.scopes is not None else READ_SCOPES & available
    if not scopes or not scopes <= available:
        raise HTTPException(status_code=403, detail="不能签发超出当前账号权限的 MCP 凭据")
    raw_token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = utc_now()
    row = McpAccessToken(
        user_id=context.user.id,
        name=payload.name,
        token_hash=token_digest(raw_token),
        token_prefix=raw_token[:15],
        scopes=",".join(sorted(scopes)),
        token_version=int(context.user.token_version or 1),
        expires_at=now + timedelta(days=payload.expires_in_days),
        created_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"token": raw_token, "credential": _out(row), "endpoint": "/api/mcp/"}


@router.delete("/{token_id}")
def revoke_token(
    token_id: int,
    context: Annotated[AuthContext, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    row = db.scalar(
        select(McpAccessToken).where(McpAccessToken.id == token_id, McpAccessToken.user_id == context.user.id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="MCP 凭据不存在")
    if row.revoked_at is None:
        row.revoked_at = utc_now()
        db.commit()
    return {"revoked": True, "id": token_id}
