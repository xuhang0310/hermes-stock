from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.models import User
from backend.app.core.settings import settings


TOKEN_TTL_SECONDS = 60 * 60 * 12
PASSWORD_ITERATIONS = 260_000
security = HTTPBearer(auto_error=False)

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "super_admin": {"all"},
    "admin": {
        "dashboard:view",
        "agent:use",
        "data:read",
        "data:write",
        "model:manage",
        "digital_human:manage",
        "system:view",
    },
    "researcher": {"dashboard:view", "agent:use", "data:read"},
    "viewer": {"dashboard:view", "agent:use", "data:read"},
}


@dataclass(frozen=True)
class AuthContext:
    user: User
    permissions: set[str]


def _secret_key() -> bytes:
    material = settings.auth_secret or settings.db_password or settings.llm_api_key or "hermes-stock-dev-secret"
    return material.encode("utf-8")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        algorithm, iterations, salt, expected = password_hash.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), _unb64(salt), int(iterations))
    return hmac.compare_digest(_b64(digest), expected)


def permissions_for(user: User) -> set[str]:
    permissions = set(ROLE_PERMISSIONS.get(user.role, ROLE_PERMISSIONS["viewer"]))
    extra = {item.strip() for item in (user.permission_scope or "").split(",") if item.strip()}
    permissions.update(extra)
    if "all" in permissions:
        return {"all"}
    return permissions


def user_payload(context: AuthContext) -> dict:
    user = context.user
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "permissions": sorted(context.permissions),
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }


def create_access_token(user: User) -> str:
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "iat": now,
        "exp": now + TOKEN_TTL_SECONDS,
    }
    signing_input = (
        f"{_b64(json.dumps(header, separators=(',', ':')).encode())}."
        f"{_b64(json.dumps(payload, separators=(',', ':')).encode())}"
    )
    signature = hmac.new(_secret_key(), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(signature)}"


def decode_access_token(token: str) -> dict:
    try:
        header_b64, payload_b64, signature_b64 = token.split(".", 2)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="登录凭证格式不正确") from exc

    signing_input = f"{header_b64}.{payload_b64}"
    expected = _b64(hmac.new(_secret_key(), signing_input.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature_b64, expected):
        raise HTTPException(status_code=401, detail="登录凭证签名无效")
    try:
        payload = json.loads(_unb64(payload_b64))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=401, detail="登录凭证内容无效") from exc
    if int(payload.get("exp", 0)) < int(time.time()):
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    return payload


def has_permission(context: AuthContext, permission: str) -> bool:
    return "all" in context.permissions or permission in context.permissions


def require_permission(permission: str) -> Callable[[AuthContext], AuthContext]:
    def dependency(context: Annotated[AuthContext, Depends(get_current_user)]) -> AuthContext:
        if not has_permission(context, permission):
            raise HTTPException(status_code=403, detail="当前账号没有该操作权限")
        return context

    return dependency


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
    db: Annotated[Session, Depends(get_db)],
) -> AuthContext:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="请先登录")
    payload = decode_access_token(credentials.credentials)
    user = db.get(User, int(payload.get("sub") or 0))
    if user is None or not user.is_enabled:
        raise HTTPException(status_code=401, detail="账号不存在或已停用")
    return AuthContext(user=user, permissions=permissions_for(user))


def authenticate_user(db: Session, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.username == username).limit(1))
    if user is None or not user.is_enabled:
        return None
    if not verify_password(password, user.password_hash):
        return None
    user.last_login_at = datetime.now()
    db.commit()
    db.refresh(user)
    return user
