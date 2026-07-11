from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from datetime import datetime
from urllib.parse import urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.models import Notification, NotifyChannel, SystemLog
from backend.app.core.settings import settings


ALERT_CHANNEL_KEY = "feishu-alert"
DAILY_CHANNEL_KEY = "feishu-daily"
BUILTIN_CHANNEL_KEYS = (ALERT_CHANNEL_KEY, DAILY_CHANNEL_KEY)

FEISHU_CONTENT_LIMIT = 4000
RATE_LIMIT_PER_MINUTE = 20
_RETRYABLE_STATUS = {500, 502, 503, 504}
# 只允许飞书/Lark 官方 webhook 域名，防止 test/保存端点被用作 SSRF 探测
ALLOWED_WEBHOOK_HOSTS = {"open.feishu.cn", "open.larksuite.com"}

_rate_lock = threading.Lock()
_rate_history: dict[str, list[float]] = {}


def _fernet() -> Fernet:
    material = settings.webhook_enc_key or (
        f"hermes-stock-webhook::{settings.auth_secret or settings.db_password or 'dev-secret'}"
    )
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_value(raw: str | None) -> str | None:
    if not raw or not raw.strip():
        return None
    return _fernet().encrypt(raw.strip().encode("utf-8")).decode("ascii")


def decrypt_value(cipher: str | None) -> str | None:
    if not cipher:
        return None
    try:
        return _fernet().decrypt(cipher.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def is_cipher_invalid(cipher: str | None) -> bool:
    """密文存在但解密失败（多为加密密钥变更所致），需要与"未配置"区分开。"""
    return bool(cipher) and decrypt_value(cipher) is None


def validate_webhook_url(url: str) -> str | None:
    """校验 webhook 地址；合法返回 None，非法返回中文错误文案。"""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return "Webhook 地址格式不正确"
    if parsed.scheme != "https":
        return "Webhook 地址必须使用 https"
    if parsed.hostname not in ALLOWED_WEBHOOK_HOSTS:
        return "Webhook 域名必须是 open.feishu.cn 或 open.larksuite.com"
    return None


def mask_webhook(url: str | None) -> str | None:
    if not url:
        return None
    if "/hook/" in url:
        prefix = url.split("/hook/", 1)[0]
        return f"{prefix}/hook/****{url[-4:]}"
    if len(url) <= 24:
        return "****"
    return f"{url[:20]}****{url[-4:]}"


def _gen_sign(secret: str, timestamp: int) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _build_payload(title: str, content: str, secret: str | None, purpose: str) -> dict:
    text = content.strip()
    if len(text) > FEISHU_CONTENT_LIMIT:
        text = text[:FEISHU_CONTENT_LIMIT] + "\n……内容过长已截断，全文见系统内记录"
    payload: dict = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title[:120]},
                "template": "red" if purpose == "alert" else "blue",
            },
            "elements": [{"tag": "markdown", "content": text}],
        },
    }
    if secret:
        timestamp = int(time.time())
        payload["timestamp"] = str(timestamp)
        payload["sign"] = _gen_sign(secret, timestamp)
    return payload


def _acquire_rate_slot(channel_key: str) -> bool:
    now = time.monotonic()
    with _rate_lock:
        history = _rate_history.setdefault(channel_key, [])
        history[:] = [item for item in history if now - item < 60]
        if len(history) >= RATE_LIMIT_PER_MINUTE:
            return False
        history.append(now)
        return True


def _post_with_retry(url: str, payload: dict, attempts: int = 3) -> tuple[bool, int | None, str]:
    """仅对网络错误与 5xx 退避重试；飞书业务错误码（签名错等）不重试。"""
    last_error = ""
    status_code: int | None = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(2 ** (attempt - 1))
        try:
            response = httpx.post(url, json=payload, timeout=httpx.Timeout(10.0, connect=5.0))
        except Exception as exc:  # noqa: BLE001
            last_error = f"网络错误: {str(exc)[:200]}"
            continue
        status_code = response.status_code
        if response.status_code in _RETRYABLE_STATUS:
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            continue
        if response.status_code != 200:
            return False, status_code, f"HTTP {response.status_code}: {response.text[:300]}"
        try:
            data = response.json()
        except json.JSONDecodeError:
            data = {}
        code = data.get("code", data.get("StatusCode", 0))
        if code in (0, "0", None):
            return True, status_code, ""
        return False, status_code, f"飞书返回错误 code={code}: {str(data.get('msg') or data)[:300]}"
    return False, status_code, last_error or "发送失败"


def _log(db: Session, level: str, message: str, context: dict | None = None) -> None:
    # db 参数保留以兼容调用点；实际改由统一日志中枢独立事务写入 + 落进程日志文件
    from backend.app.core.applog import log_event

    log_event("feishu", level, message, context=context)


def get_channel(db: Session, channel_key: str) -> NotifyChannel | None:
    return db.scalar(select(NotifyChannel).where(NotifyChannel.channel_key == channel_key).limit(1))


def send_feishu(
    db: Session,
    channel_key: str,
    title: str,
    content: str,
    *,
    event_id: str | None = None,
    report_id: str | None = None,
) -> Notification:
    """向指定通道推送消息。先落 pending 审计记录再发送，任何失败都不抛异常、只写 failed。"""
    notification = Notification(
        channel=f"feishu:{channel_key}",
        title=title[:300],
        content=content,
        event_id=event_id,
        report_id=report_id,
        status="pending",
    )
    db.add(notification)
    db.flush()

    channel = get_channel(db, channel_key)
    webhook_url = decrypt_value(channel.webhook_url_cipher) if channel else None
    if channel is None or channel.status != "enabled" or not webhook_url:
        notification.status = "failed"
        if channel is not None and is_cipher_invalid(channel.webhook_url_cipher):
            notification.error = "Webhook 密文解密失败：加密密钥可能已变更（STOCK_WEBHOOK_ENC_KEY 或其派生来源），请重新保存通道配置"
            _log(db, "ERROR", f"飞书通道 {channel_key} 密文解密失败，疑似加密密钥变更", {"title": title[:80]})
        else:
            notification.error = "通道未配置或已停用"
            _log(db, "WARNING", f"飞书推送跳过：通道 {channel_key} 未配置或已停用", {"title": title[:80]})
        db.commit()
        return notification

    if not _acquire_rate_slot(channel_key):
        notification.status = "failed"
        notification.error = f"触发本地限流（每分钟最多 {RATE_LIMIT_PER_MINUTE} 条），消息未发送"
        _log(db, "ERROR", f"飞书推送限流：通道 {channel_key}", {"title": title[:80]})
        db.commit()
        return notification

    secret = decrypt_value(channel.secret_cipher)
    purpose = channel.purpose
    # 先提交 pending 审计行并释放数据库连接，再做外部 HTTP 调用：
    # 避免重试窗口（最长约 33 秒）占用连接池，且进程中途崩溃也能留下审计痕迹
    db.commit()

    payload = _build_payload(title, content, secret, purpose)
    ok, status_code, error = _post_with_retry(webhook_url, payload)
    if ok:
        notification.status = "sent"
        notification.sent_at = datetime.now()
        channel.last_ok_at = notification.sent_at
        channel.last_error = None
    else:
        notification.status = "failed"
        notification.error = error
        channel.last_error = error
        _log(
            db,
            "ERROR",
            f"飞书推送失败：通道 {channel_key}",
            {"title": title[:80], "status_code": status_code, "error": error[:300]},
        )
    db.commit()
    return notification


def send_alert(db: Session, title: str, content: str, *, event_id: str | None = None) -> Notification:
    """告警群直推。规则引擎 L0 走这里，纯模板字符串，天然不经 LLM。"""
    return send_feishu(db, ALERT_CHANNEL_KEY, title, content, event_id=event_id)


def send_daily(db: Session, title: str, content: str, *, report_id: str | None = None) -> Notification:
    """日报群推送：简报、快评、日报等 Agent 产出。"""
    return send_feishu(db, DAILY_CHANNEL_KEY, title, content, report_id=report_id)


def test_channel(
    db: Session,
    *,
    channel_key: str | None = None,
    webhook_url: str | None = None,
    secret: str | None = None,
) -> dict:
    """测试推送。表单字段有值用表单值（未保存先测），留空则逐字段回落到已保存通道配置。"""
    channel = get_channel(db, channel_key) if channel_key else None
    if channel_key and channel is None:
        return {"ok": False, "error": "通道不存在", "latency_ms": 0}
    resolved_url = (webhook_url or "").strip() or (decrypt_value(channel.webhook_url_cipher) if channel else None) or ""
    resolved_secret = (secret or "").strip() or (decrypt_value(channel.secret_cipher) if channel else None)
    purpose = channel.purpose if channel else "daily"
    if not resolved_url:
        if channel is not None and is_cipher_invalid(channel.webhook_url_cipher):
            return {"ok": False, "error": "Webhook 密文解密失败：加密密钥可能已变更，请重新填写并保存", "latency_ms": 0}
        return {"ok": False, "error": "Webhook 地址不能为空，请先填写或保存通道配置", "latency_ms": 0}
    url_error = validate_webhook_url(resolved_url)
    if url_error:
        return {"ok": False, "error": url_error, "latency_ms": 0}

    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = _build_payload(
        "【测试】SixRabbits Stock 通道连通性验证",
        f"这是一条测试消息，发送时间 {now_text}。收到即代表通道配置正确。",
        resolved_secret,
        purpose,
    )
    started = time.monotonic()
    ok, status_code, error = _post_with_retry(resolved_url, payload, attempts=1)
    latency_ms = int((time.monotonic() - started) * 1000)
    _log(
        db,
        "INFO" if ok else "WARNING",
        f"飞书通道测试{'成功' if ok else '失败'}：{channel_key or '未保存表单'}",
        {"status_code": status_code, "error": error[:200] if error else None, "latency_ms": latency_ms},
    )
    db.commit()
    if ok:
        return {"ok": True, "message": "测试消息已发送，请到群里确认", "status_code": status_code, "latency_ms": latency_ms}
    return {"ok": False, "error": error, "status_code": status_code, "latency_ms": latency_ms}
