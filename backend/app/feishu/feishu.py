from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from datetime import datetime
from urllib.parse import urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.models import Notification, NotifyChannel
from backend.app.core.settings import settings


SCHEDULE_CHANNEL_KEY = "feishu-daily"
ALERT_CHANNEL_KEY = SCHEDULE_CHANNEL_KEY
DAILY_CHANNEL_KEY = SCHEDULE_CHANNEL_KEY
LEGACY_CHANNEL_KEYS = ("feishu-alert",)
BUILTIN_CHANNEL_KEYS = (SCHEDULE_CHANNEL_KEY,)

FEISHU_CONTENT_LIMIT = 3600
FEISHU_COMPONENTS_PER_CARD = 3
FEISHU_MAX_CARDS_PER_MESSAGE = 18
FEISHU_MAX_TABLES_PER_CARD = 5
RATE_LIMIT_PER_MINUTE = 20
_RETRYABLE_STATUS = {500, 502, 503, 504}
_CARD_TEMPLATES = {
    "blue", "wathet", "turquoise", "green", "yellow", "orange", "red",
    "carmine", "violet", "purple", "indigo", "grey",
}
# 只允许飞书/Lark 官方 webhook 域名，防止 test/保存端点被用作 SSRF 探测
ALLOWED_WEBHOOK_HOSTS = {"open.feishu.cn", "open.larksuite.com"}

_rate_lock = threading.Lock()
_rate_history: dict[str, list[float]] = {}


_FERNET_CACHE: Fernet | None = None


def _fernet() -> Fernet:
    global _FERNET_CACHE
    if _FERNET_CACHE is not None:
        return _FERNET_CACHE
    if settings.webhook_enc_key:
        material = settings.webhook_enc_key
    else:
        import logging
        _log = logging.getLogger("hermes.feishu")
        _log.warning(
            "STOCK_WEBHOOK_ENC_KEY 未设置！将使用进程内随机密钥加密 webhook。"
            "重启后所有已加密的 webhook URL 将无法解密，需重新保存。"
            "请在 backend/.env 中设置 STOCK_WEBHOOK_ENC_KEY=<至少32字符的随机串>。"
        )
        material = secrets.token_urlsafe(32)
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    _FERNET_CACHE = Fernet(base64.urlsafe_b64encode(digest))
    return _FERNET_CACHE


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


def _compact_text(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


# ── 飞书 markdown 清洗 ──────────────────────────────────────────────
# 飞书卡片的 markdown 渲染与标准 CommonMark 有差异：不支持 HTML 块级标签、
# 部分特殊字符需要转义。此处在保留语义的前提下做轻量清洗，避免原始 markdown
# 在飞书中渲染为一堆裸标记。
_FEISHU_STRIP_TAGS = re.compile(
    r"</?(?:details|summary|div|span|center|table|thead|tbody|tr|th|td)"
    r"(?:\s[^>]*)?>",
    re.IGNORECASE,
)
_FEISHU_IMG_TAG = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def _split_markdown_table_row(line: str) -> list[str] | None:
    """Split a table row without treating escaped or code-span pipes as separators."""
    stripped = line.strip()
    if "|" not in stripped:
        return None
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith(r"\|"):
        stripped = stripped[:-1]

    cells: list[str] = []
    current: list[str] = []
    escaped = False
    in_code = False
    for char in stripped:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            current.append(char)
            continue
        if char == "`":
            in_code = not in_code
            current.append(char)
            continue
        if char == "|" and not in_code:
            cells.append("".join(current).strip().replace(r"\|", "|"))
            current = []
            continue
        current.append(char)
    cells.append("".join(current).strip().replace(r"\|", "|"))
    return cells if len(cells) >= 2 else None


def _is_markdown_table_separator(line: str) -> bool:
    """Return whether a line is the separator row of a Markdown table."""
    cells = _split_markdown_table_row(line)
    return bool(cells) and all(
        re.fullmatch(r"\s*:?-{3,}:?\s*", cell) is not None
        for cell in cells
    )


def _format_table_for_mobile(headers: list[str], rows: list[list[str]]) -> str:
    """Render a table as vertical blocks that Feishu mobile does not clip."""
    blocks: list[str] = []
    width = len(headers)
    for row_index, raw_row in enumerate(rows, start=1):
        row = (raw_row + [""] * width)[:width]
        if width == 2:
            label = row[0] or f"第 {row_index} 项"
            blocks.append(f"**{label}**\n{row[1] or '-'}")
            continue

        title = row[0] or f"第 {row_index} 项"
        lines = [f"**{title}**"]
        lines.extend(
            f"**{header or f'字段 {column_index + 1}'}：** {value or '-'}"
            for column_index, (header, value) in enumerate(zip(headers[1:], row[1:]), start=1)
        )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _linearize_markdown_tables(text: str) -> str:
    """Replace content tables with mobile-safe blocks while preserving every cell."""
    lines = text.splitlines()
    output: list[str] = []
    index = 0
    while index < len(lines):
        if index + 1 >= len(lines) or not _is_markdown_table_separator(lines[index + 1]):
            output.append(lines[index])
            index += 1
            continue

        headers = _split_markdown_table_row(lines[index])
        if not headers:
            output.append(lines[index])
            index += 1
            continue

        rows: list[list[str]] = []
        cursor = index + 2
        while cursor < len(lines):
            row = _split_markdown_table_row(lines[cursor])
            if not row:
                break
            rows.append(row)
            cursor += 1

        if not rows:
            output.extend(lines[index:cursor])
        else:
            output.append(_format_table_for_mobile(headers, rows))
        index = cursor
    return "\n".join(output)


def _sanitize_markdown_for_feishu(text: str) -> str:
    """清洗 markdown 使之在飞书卡片中尽量正确渲染。

    处理：
    1. CRLF → LF（避免段落分割失效）
    2. 去除飞书不支持的 HTML 块级标签（保留行内 <font>/<a>/<at> 等）
    3. Markdown 表格转为纵向信息块，避免移动端长单元格被裁剪
    4. 图片语法 ![alt](url) 转为飞书可识别的链接（飞书不支持标准图片语法）
    5. 连续空行压缩为两段（飞书渲染多段间距无差异，减少无效字符）
    """
    # 1. 统一换行
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Keep table cells on one line before removing unsupported block HTML.
    text = re.sub(r"<br\s*/?>", " / ", text, flags=re.IGNORECASE)
    # 2. 去除不支持的 HTML 块级标签
    text = _FEISHU_STRIP_TAGS.sub("", text)
    # 3. 飞书移动端会裁剪 Markdown 表格的长单元格，改为纵向信息块完整展示。
    text = _linearize_markdown_tables(text)
    # 4. 图片 → 链接（飞书 markdown 组件不渲染标准图片语法）
    text = _FEISHU_IMG_TAG.sub(r"[📷 \1](\2)", text)
    # 5. 连续 3+ 空行 → 2 空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Do not render an entire model response as a code block merely because
    # the model wrapped it in a markdown fence.
    stripped = text.strip()
    outer_fence = re.match(r"^```(?:markdown|md)?\s*\n([\s\S]*)\n```$", stripped, re.IGNORECASE)
    return outer_fence.group(1).strip() if outer_fence else stripped


# ── markdown 分片 ────────────────────────────────────────────────────
_MIN_CHUNK_FRACTION = 3  # 分片最小长度 = limit // 此值，避免碎片


def split_markdown_content(
    text: str,
    limit: int = FEISHU_CONTENT_LIMIT,
    max_chunks: int | None = FEISHU_COMPONENTS_PER_CARD * FEISHU_MAX_CARDS_PER_MESSAGE,
) -> list[str]:
    """将长 markdown 拆分为多个分片，每片不超过 limit 字符。

    拆分策略按优先级：
    1. 段落边界（\\n\\n）
    2. 代码块闭合边界（配对 ``` 之后）
    3. 单行换行
    4. 硬截断（兜底）

    返回最多 max_chunks 片；超出时最后一片追加截断提示。
    """
    if limit <= 0:
        return [text] if text else [""]
    text = _sanitize_markdown_for_feishu(text)
    if len(text) <= limit:
        return [text] if text else [""]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        # 达到最大分片数：最后一片截断并提示
        if max_chunks is not None and len(chunks) >= max_chunks - 1:
            tail = remaining[:limit]
            chunks.append(tail.rstrip() + "\n\n……内容过长，已截断。全文见系统内记录。")
            break

        min_chunk = limit // _MIN_CHUNK_FRACTION

        # 1. 段落边界（\n\n）
        split_at = remaining.rfind("\n\n", 0, limit)

        # 2. 代码块闭合边界 —— 必须找到配对的 ``` 才在之后切
        if split_at < min_chunk:
            split_at = _find_code_block_boundary(remaining, limit, min_chunk)

        # 3. 单行换行
        if split_at < min_chunk:
            split_at = remaining.rfind("\n", 0, limit)

        # 4. 兜底：硬截断
        if split_at < min_chunk:
            split_at = limit

        chunk = remaining[:split_at].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()

    return [c for c in chunks if c]  # 过滤空片


def _find_code_block_boundary(text: str, limit: int, min_chunk: int) -> int:
    """在 [0, limit) 内找到最后一个 *闭合* 的 ``` 后的换行位置。

    通过统计 ``` 出现次数的奇偶性判断开/闭：奇数个 = 当前找到的最后一个是闭合。
    找不到配对闭合则返回 -1。
    """
    # 找到 limit 内所有 ``` 的位置
    fences: list[int] = []
    start = 0
    while True:
        pos = text.find("```", start, limit)
        if pos == -1:
            break
        fences.append(pos)
        start = pos + 3
    if len(fences) < 2:
        return -1  # 没有配对的闭合
    # 最后一个 ``` 是闭合（假设文本从代码块外部开始，奇偶性成立）
    last_fence = fences[-1]
    # 确认是闭合：前面有偶数个 fence（0-indexed 即 len-1 为奇数索引）
    if len(fences) % 2 == 0:
        # 偶数个 fence：最后一个是闭合
        after = text.find("\n", last_fence)
        if after != -1 and after <= limit:
            return after if after >= min_chunk else -1
    return -1


def _count_markdown_tables(text: str) -> int:
    """Count tables the Feishu Markdown renderer expands into table components."""
    lines = text.splitlines()
    return sum(
        1
        for index, line in enumerate(lines)
        if index > 0
        and "|" in lines[index - 1]
        and _is_markdown_table_separator(line)
    )


def _split_table_dense_chunk(text: str, max_tables: int) -> list[str]:
    """Split a chunk so no part can expand to more than ``max_tables`` tables."""
    if max_tables <= 0 or _count_markdown_tables(text) <= max_tables:
        return [text]

    parts: list[str] = []
    remaining = text
    while _count_markdown_tables(remaining) > max_tables:
        lines = remaining.splitlines()
        table_headers = [
            index - 1
            for index, line in enumerate(lines)
            if index > 0
            and "|" in lines[index - 1]
            and _is_markdown_table_separator(line)
        ]
        split_at = table_headers[max_tables]
        head = "\n".join(lines[:split_at]).strip()
        if not head:
            break
        parts.append(head)
        remaining = "\n".join(lines[split_at:]).strip()

    if remaining:
        parts.append(remaining)
    return parts or [text]


def _paginate_markdown(
    text: str,
    *,
    has_metadata_table: bool,
) -> list[list[str]]:
    """Paginate by characters, component count and Feishu's five-table limit."""
    content_table_limit = FEISHU_MAX_TABLES_PER_CARD - (1 if has_metadata_table else 0)
    chunks = [
        part
        for chunk in split_markdown_content(text)
        for part in _split_table_dense_chunk(chunk, content_table_limit)
    ]

    pages: list[list[str]] = []
    current: list[str] = []
    current_tables = 1 if has_metadata_table else 0

    for chunk in chunks:
        chunk_tables = _count_markdown_tables(chunk)
        if current and (
            len(current) >= FEISHU_COMPONENTS_PER_CARD
            or current_tables + chunk_tables > FEISHU_MAX_TABLES_PER_CARD
        ):
            pages.append(current)
            current = []
            current_tables = 0

        current.append(chunk)
        current_tables += chunk_tables

    if current:
        pages.append(current)
    return pages or [[text]]


def markdown_elements(text: str, *, start_index: int = 0) -> list[dict]:
    """将 markdown 文本转为飞书卡片 markdown 元素列表（自动分片）。"""
    return [
        {
            "tag": "markdown",
            "element_id": f"content_{start_index + index + 1:03d}",
            "content": chunk,
            "text_size": "normal",
            "text_align": "left",
            "margin": "0px 0px 0px 0px",
        }
        for index, chunk in enumerate(split_markdown_content(text))
    ]


def _metadata_markdown(metadata: list[tuple[str, str]] | None) -> str:
    items = [
        (_compact_text(label, 20), _compact_text(value, 100) or "-")
        for label, value in (metadata or [])[:6]
    ]
    if not items:
        return ""
    rows = ["| 项目 | 内容 | 项目 | 内容 |", "| --- | --- | --- | --- |"]
    for index in range(0, len(items), 2):
        left = items[index]
        right = items[index + 1] if index + 1 < len(items) else ("", "")
        escaped = [value.replace("|", "&#124;") for value in (*left, *right)]
        rows.append("| " + " | ".join(escaped) + " |")
    return "\n".join(rows)


def _card_payload(
    title: str,
    content_elements: list[dict],
    *,
    purpose: str,
    metadata: list[tuple[str, str]] | None,
    footer: str | None,
    template: str | None,
    page: int,
    page_count: int,
) -> dict:
    card_template = template if template in _CARD_TEMPLATES else ("red" if purpose == "alert" else "blue")
    elements: list[dict] = []
    metadata_text = _metadata_markdown(metadata) if page == 1 else ""
    if metadata_text:
        elements.extend(
            [
                {
                    "tag": "markdown",
                    "element_id": "metadata",
                    "content": metadata_text,
                    "text_size": "normal",
                    "text_align": "left",
                },
                {"tag": "hr"},
            ]
        )
    elements.extend(content_elements)
    footer_text = footer or (
        "SixRabbits Stock | 系统告警，请及时核查"
        if purpose == "alert"
        else "SixRabbits Stock | 自动生成，仅供研究与复盘，不构成投资建议"
    )
    elements.extend(
        [
            {"tag": "hr"},
            {
                "tag": "markdown",
                "element_id": f"footer_{page:02d}",
                "content": footer_text[:200],
                "text_size": "notation",
                "text_align": "left",
            },
        ]
    )
    page_title = title if page_count == 1 else f"{title}（{page}/{page_count}）"
    return {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": True,
            "enable_forward": True,
            "update_multi": True,
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 12px 12px",
            "elements": elements,
        },
        "header": {
            "title": {"tag": "plain_text", "content": page_title[:120]},
            "template": card_template,
        },
    }


def _build_payloads(
    title: str,
    content: str,
    secret: str | None,
    purpose: str,
    *,
    metadata: list[tuple[str, str]] | None = None,
    footer: str | None = None,
    template: str | None = None,
) -> list[dict]:
    text = content.strip() or "本次任务未返回正文。"
    pages = _paginate_markdown(text, has_metadata_table=bool(metadata))
    payloads: list[dict] = []
    page_count = len(pages)
    element_index = 0
    for page_index, page_chunks in enumerate(pages, start=1):
        content_elements: list[dict] = []
        for chunk in page_chunks:
            element_index += 1
            content_elements.append(
                {
                    "tag": "markdown",
                    "element_id": f"content_{element_index:03d}",
                    "content": chunk,
                    "text_size": "normal",
                    "text_align": "left",
                    "margin": "0px 0px 0px 0px",
                }
            )
        payload: dict = {
            "msg_type": "interactive",
            "card": _card_payload(
                title,
                content_elements,
                purpose=purpose,
                metadata=metadata,
                footer=footer,
                template=template,
                page=page_index,
                page_count=page_count,
            ),
        }
        if secret:
            timestamp = int(time.time())
            payload["timestamp"] = str(timestamp)
            payload["sign"] = _gen_sign(secret, timestamp)
        payloads.append(payload)
    return payloads


def _build_payload(
    title: str,
    content: str,
    secret: str | None,
    purpose: str,
    *,
    metadata: list[tuple[str, str]] | None = None,
    footer: str | None = None,
    template: str | None = None,
) -> dict:
    return _build_payloads(
        title,
        content,
        secret,
        purpose,
        metadata=metadata,
        footer=footer,
        template=template,
    )[0]


def _acquire_rate_slots(channel_key: str, count: int = 1) -> bool:
    now = time.monotonic()
    with _rate_lock:
        history = _rate_history.setdefault(channel_key, [])
        history[:] = [item for item in history if now - item < 60]
        requested = max(1, count)
        if len(history) + requested > RATE_LIMIT_PER_MINUTE:
            return False
        history.extend([now] * requested)
        return True


def _acquire_rate_slot(channel_key: str) -> bool:
    return _acquire_rate_slots(channel_key, 1)


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
    card_metadata: list[tuple[str, str]] | None = None,
    card_footer: str | None = None,
    card_template: str | None = None,
    card_kind: str | None = None,
) -> Notification:
    """向指定通道推送消息。先落 pending 审计记录再发送，任何失败都不抛异常、只写 failed。"""
    if report_id or event_id:
        identity_filter = (
            Notification.report_id == report_id
            if report_id
            else Notification.event_id == event_id
        )
        existing = db.scalar(
            select(Notification)
            .where(
                Notification.channel == f"feishu:{channel_key}",
                identity_filter,
                Notification.status.in_(("pending", "sent")),
            )
            .order_by(Notification.id.desc())
            .limit(1)
        )
        if existing is not None:
            return existing

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

    secret = decrypt_value(channel.secret_cipher)
    purpose = card_kind or channel.purpose
    # 先提交 pending 审计行并释放数据库连接，再做外部 HTTP 调用：
    # 避免重试窗口（最长约 33 秒）占用连接池，且进程中途崩溃也能留下审计痕迹
    db.commit()

    payloads = _build_payloads(
        title,
        content,
        secret,
        purpose,
        metadata=card_metadata,
        footer=card_footer,
        template=card_template,
    )
    if not _acquire_rate_slots(channel_key, len(payloads)):
        notification.status = "failed"
        notification.error = (
            f"触发本地限流（每分钟最多 {RATE_LIMIT_PER_MINUTE} 条），"
            f"本次长消息需要连续发送 {len(payloads)} 页，消息未发送"
        )
        _log(
            db,
            "ERROR",
            f"飞书推送限流：通道 {channel_key}",
            {"title": title[:80], "total_pages": len(payloads)},
        )
        db.commit()
        return notification

    sent_pages = 0
    status_code: int | None = None
    error = ""
    for page_index, payload in enumerate(payloads, start=1):
        ok, status_code, error = _post_with_retry(webhook_url, payload)
        if not ok:
            error = f"第 {page_index}/{len(payloads)} 页发送失败：{error}"
            break
        sent_pages += 1
        if page_index < len(payloads):
            time.sleep(0.15)

    if sent_pages == len(payloads):
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
            {
                "title": title[:80],
                "status_code": status_code,
                "error": error[:300],
                "sent_pages": sent_pages,
                "total_pages": len(payloads),
            },
        )
    db.commit()
    return notification


def send_alert(db: Session, title: str, content: str, *, event_id: str | None = None) -> Notification:
    """系统异常推到唯一任务群；使用红色告警卡片，纯模板字符串不经 LLM。"""
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return send_feishu(
        db,
        ALERT_CHANNEL_KEY,
        title,
        content,
        event_id=event_id,
        card_kind="alert",
        card_template="red",
        card_metadata=[("消息类型", "系统告警"), ("发送时间", now_text)],
        card_footer="SixRabbits Stock | 系统告警，请及时核查",
    )


def send_daily(db: Session, title: str, content: str, *, report_id: str | None = None) -> Notification:
    """唯一任务群推送：简报、快评、日报等 Agent 产出。"""
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return send_feishu(
        db,
        DAILY_CHANNEL_KEY,
        title,
        content,
        report_id=report_id,
        card_metadata=[("消息类型", "定时任务"), ("生成时间", now_text)],
    )


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
        "SixRabbits Stock | 飞书任务群连接测试",
        "配置校验已经完成。收到这张卡片，代表 Webhook、签名密钥和交互式卡片格式均可正常使用。",
        resolved_secret,
        purpose,
        metadata=[("消息类型", "连接测试"), ("发送时间", now_text), ("卡片格式", "Interactive Card")],
        footer="SixRabbits Stock | 定时任务统一推送通道",
        template="turquoise",
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
    if channel is not None:
        if ok:
            channel.last_ok_at = datetime.now()
            channel.last_error = None
        else:
            channel.last_error = error
    db.commit()
    if ok:
        return {"ok": True, "message": "测试消息已发送，请到群里确认", "status_code": status_code, "latency_ms": latency_ms}
    return {"ok": False, "error": error, "status_code": status_code, "latency_ms": latency_ms}
