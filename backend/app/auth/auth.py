from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
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


PASSWORD_ITERATIONS = 260_000
security = HTTPBearer(auto_error=False)

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "super_admin": {"all"},
    "admin": {
        "dashboard:view",
        "agent:use",
        "agent:manage",
        "data:read",
        "data:write",
        "schedule:manage",
        "user:manage",
        "model:manage",
        "digital_human:manage",
        "system:view",
    },
    "researcher": {
        "dashboard:view",
        "agent:use",
        "agent:manage",
        "data:read",
        "data:write",
        "schedule:manage",
    },
    "viewer": {"dashboard:view", "agent:use", "data:read"},
}


@dataclass(frozen=True)
class AuthContext:
    user: User
    permissions: set[str]


_AUTH_SECRET_CACHE: bytes | None = None
_LOGIN_LIMIT_LOCK = threading.Lock()
_LOGIN_LIMIT_LOCAL: dict[str, tuple[int, float]] = {}
_LOGIN_LIMIT_REDIS_UNAVAILABLE_UNTIL = 0.0


def _secret_key() -> bytes:
    global _AUTH_SECRET_CACHE
    if _AUTH_SECRET_CACHE is not None:
        return _AUTH_SECRET_CACHE
    if settings.auth_secret:
        _AUTH_SECRET_CACHE = settings.auth_secret.encode("utf-8")
        return _AUTH_SECRET_CACHE
    # 未配置 STOCK_AUTH_SECRET → 本次进程内随机生成，避免回退到 DB 密码或硬编码默认值。
    # 代价：重启后所有已签发 token 失效（用户需重新登录）。
    import logging
    _log = logging.getLogger("hermes.auth")
    _log.warning(
        "STOCK_AUTH_SECRET 未设置！已使用随机密钥，重启后所有会话将失效。"
        "请在 backend/.env 中设置 STOCK_AUTH_SECRET=<至少32字符的随机串> 以保持会话持久化。"
    )
    _AUTH_SECRET_CACHE = secrets.token_bytes(32)
    return _AUTH_SECRET_CACHE


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


def access_token_ttl_seconds(*, remember_me: bool = False) -> int:
    if remember_me:
        return max(1, settings.auth_remember_token_ttl_days) * 24 * 60 * 60
    return max(1, settings.auth_token_ttl_hours) * 60 * 60


def create_access_token(user: User, *, remember_me: bool = False) -> str:
    now = int(time.time())
    ttl_seconds = access_token_ttl_seconds(remember_me=remember_me)
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "ver": int(user.token_version or 1),
        "iat": now,
        "exp": now + ttl_seconds,
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
    if int(payload.get("ver", 1)) != int(user.token_version or 1):
        raise HTTPException(status_code=401, detail="登录凭证已失效，请重新登录")
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


def _login_limit_key(kind: str, value: str) -> str:
    digest = hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()
    return f"hermes:auth:login:{kind}:{digest}"


def _login_limit_redis():
    global _LOGIN_LIMIT_REDIS_UNAVAILABLE_UNTIL
    if not settings.agent_redis_url or time.monotonic() < _LOGIN_LIMIT_REDIS_UNAVAILABLE_UNTIL:
        return None
    try:
        from redis import Redis

        client = Redis.from_url(
            settings.agent_redis_url,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
            decode_responses=True,
        )
        client.ping()
        return client
    except Exception:  # noqa: BLE001
        _LOGIN_LIMIT_REDIS_UNAVAILABLE_UNTIL = time.monotonic() + 30
        return None


def login_retry_after(*, username: str, client_ip: str) -> int:
    keys = (
        _login_limit_key("account", username),
        _login_limit_key("ip", client_ip),
    )
    limit = max(1, settings.auth_login_max_attempts)
    client = _login_limit_redis()
    if client is not None:
        try:
            values = client.mget(keys)
            retry_after = 0
            for key, value in zip(keys, values, strict=True):
                if int(value or 0) >= limit:
                    retry_after = max(retry_after, max(1, int(client.ttl(key))))
            return retry_after
        except Exception:  # noqa: BLE001
            pass

    now = time.monotonic()
    retry_after = 0
    with _LOGIN_LIMIT_LOCK:
        for key in keys:
            count, expires_at = _LOGIN_LIMIT_LOCAL.get(key, (0, 0.0))
            if expires_at <= now:
                _LOGIN_LIMIT_LOCAL.pop(key, None)
            elif count >= limit:
                retry_after = max(retry_after, max(1, int(expires_at - now)))
    return retry_after


def record_login_failure(*, username: str, client_ip: str) -> None:
    keys = (
        _login_limit_key("account", username),
        _login_limit_key("ip", client_ip),
    )
    window = max(30, settings.auth_login_window_seconds)
    client = _login_limit_redis()
    if client is not None:
        try:
            pipeline = client.pipeline(transaction=True)
            for key in keys:
                pipeline.incr(key)
                pipeline.expire(key, window, nx=True)
            pipeline.execute()
            return
        except Exception:  # noqa: BLE001
            pass

    now = time.monotonic()
    with _LOGIN_LIMIT_LOCK:
        if len(_LOGIN_LIMIT_LOCAL) > 10_000:
            expired = [
                key
                for key, (_, expires_at) in _LOGIN_LIMIT_LOCAL.items()
                if expires_at <= now
            ]
            for key in expired:
                _LOGIN_LIMIT_LOCAL.pop(key, None)
            if len(_LOGIN_LIMIT_LOCAL) > 10_000:
                oldest = sorted(
                    _LOGIN_LIMIT_LOCAL,
                    key=lambda item: _LOGIN_LIMIT_LOCAL[item][1],
                )[:1_000]
                for key in oldest:
                    _LOGIN_LIMIT_LOCAL.pop(key, None)
        for key in keys:
            count, expires_at = _LOGIN_LIMIT_LOCAL.get(key, (0, 0.0))
            if expires_at <= now:
                count, expires_at = 0, now + window
            _LOGIN_LIMIT_LOCAL[key] = (count + 1, expires_at)


def clear_login_failures(*, username: str) -> None:
    key = _login_limit_key("account", username)
    client = _login_limit_redis()
    if client is not None:
        try:
            client.delete(key)
        except Exception:  # noqa: BLE001
            pass
    with _LOGIN_LIMIT_LOCK:
        _LOGIN_LIMIT_LOCAL.pop(key, None)
