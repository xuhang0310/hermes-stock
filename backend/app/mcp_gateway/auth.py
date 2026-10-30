"""User-owned MCP credentials; raw credentials are never persisted."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.auth.auth import permissions_for
from backend.app.core.models import McpAccessToken, User


TOKEN_PREFIX = "hs_mcp_"
_TOKEN_PATTERN = re.compile(r"^hs_mcp_[A-Za-z0-9_-]{43}$")
SCOPE_PERMISSIONS = {
    "market:read": "data:read",
    "portfolio:read": "data:read",
    "portfolio:write": "data:write",
    "schedules:read": "schedule:manage",
    "schedules:write": "schedule:manage",
}
READ_SCOPES = frozenset({"market:read", "portfolio:read", "schedules:read"})


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def allowed_scopes(permissions: set[str] | frozenset[str]) -> frozenset[str]:
    return frozenset(
        scope for scope, permission in SCOPE_PERMISSIONS.items()
        if "all" in permissions or permission in permissions
    )


@dataclass(frozen=True)
class McpPrincipal:
    user_id: int
    token_id: int
    role: str
    permissions: frozenset[str]
    scopes: frozenset[str]

    def require_scope(self, scope: str) -> None:
        if scope not in SCOPE_PERMISSIONS or scope not in self.scopes:
            raise HTTPException(status_code=403, detail=f"MCP 凭据缺少权限：{scope}")


def authenticate_mcp_token(raw_token: str, db: Session) -> McpPrincipal:
    """Validate only MCP tokens, checking revocation and current user privileges."""
    unauthorized = HTTPException(
        status_code=401,
        detail="MCP 凭据无效、已过期或已撤销",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not _TOKEN_PATTERN.fullmatch(raw_token):
        raise unauthorized
    row = db.scalar(select(McpAccessToken).where(McpAccessToken.token_hash == token_digest(raw_token)))
    if row is None or row.revoked_at is not None or row.expires_at <= utc_now():
        raise unauthorized
    user = db.get(User, row.user_id)
    if (
        user is None or not user.is_enabled
        or row.token_version != int(user.token_version or 1)
    ):
        raise unauthorized
    permissions = frozenset(permissions_for(user))
    scopes = frozenset(row.scopes.split(",")) & allowed_scopes(permissions)
    return McpPrincipal(
        user_id=user.id,
        token_id=row.id,
        role=user.role,
        permissions=permissions,
        scopes=scopes,
    )
