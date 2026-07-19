"""L1 会话记忆 — 对话压缩模块。

移植 claw-code (rust/crates/runtime/src/compact.rs) 的对话压缩逻辑到 Python。
当对话历史超阈值时，把旧消息压成结构化 summary，保留最近 N 条原文，防 context 超长丢上下文。

核心思想（与 clawd 一致）：
  - 压缩不调 LLM，纯程序规则提取（零额外成本、零延迟）
  - 旧消息 → 结构化 summary（主题/近期请求/待办/涉及标的/当前进展）
  - 保留最近 N 条原文 verbatim，连续性不丢

MVP 说明：每次对话从 DB 读最近 HISTORY_LOAD_LIMIT 条参与判断，超 token 阈值则压缩。
clawd 的"多次压缩增量合并不丢历史"（Previously/Newly 合并）需持久化 summary 状态，留作 v2。
当前 MVP：summary 拼入 system prompt（复用 memory_context 注入通道），不持久化、不跨次合并。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.models import AiChatMessage

# ---------------------------------------------------------------------------
# 参数（沿用 clawd 默认，可调）
# ---------------------------------------------------------------------------

HISTORY_LOAD_LIMIT = 100          # 从 DB 读多少条历史参与压缩判断（替代原 limit 12 的硬截断）
PRESERVE_RECENT = 4               # 保留最近 N 条原文不压缩
MAX_ESTIMATED_TOKENS = 10_000     # 超此阈值触发压缩（约 4 万字符）
SUMMARY_MAX_CHARS = 1_200         # summary 本身的字符预算（防 summary 自爆）

# A 股代码识别：6 位数字 + 沪深北市场后缀，或纯 6 位
_SYMBOL_RE = re.compile(r"\b(\d{6})\.(SH|SZ|BJ)\b|\b(60\d{4}|00\d{4}|30\d{4}|688\d{3}|8\d{5})\b")
# 文件路径识别：带扩展名的路径
_FILE_RE = re.compile(r"(?:[\w\-./]+/)*[\w\-]+\.(?:py|ts|tsx|js|json|md|rs|sql|vue)", re.ASCII)
_PENDING_KEYWORDS = ("todo", "next", "pending", "follow up", "remaining", "待办", "未完成", "下一步", "接下来")

_SUMMARY_PREAMBLE = "【对话压缩摘要】以下为更早对话的结构化摘要，近 N 条原文已保留在后：\n"


# ---------------------------------------------------------------------------
# token 估算（与 clawd 一致：char_len/4 + 1 粗略）
# ---------------------------------------------------------------------------


def _estimate_text_tokens(text: str) -> int:
    return len(text) // 4 + 1 if text else 0


def estimate_tokens(messages: list[dict]) -> int:
    """粗略估算消息列表的 token 占用。"""
    total = 0
    for msg in messages:
        content = msg.get("content") or ""
        total += _estimate_text_tokens(content)
    return total


def should_compact(messages: list[dict], *, preserve_recent: int = PRESERVE_RECENT,
                   max_tokens: int = MAX_ESTIMATED_TOKENS) -> bool:
    """是否需要压缩：可压缩消息数 > preserve_recent 且 token 估算 ≥ 阈值。"""
    compactable = messages[:-preserve_recent] if len(messages) > preserve_recent else []
    return len(compactable) > preserve_recent and estimate_tokens(compactable) >= max_tokens


# ---------------------------------------------------------------------------
# 结构化 summary 生成（纯规则，不调 LLM）
# ---------------------------------------------------------------------------


def _truncate(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _extract_symbols(messages: list[dict]) -> list[str]:
    """从消息中提取 A 股代码（A 股化的 Key files）。"""
    found: list[str] = []
    seen: set[str] = set()
    for msg in messages:
        content = msg.get("content") or ""
        for m in _SYMBOL_RE.finditer(content):
            sym = (m.group(1) and f"{m.group(1)}.{m.group(2)}") or m.group(0)
            if sym and sym not in seen:
                seen.add(sym)
                found.append(sym)
            if len(found) >= 8:
                return found
    return found


def _extract_files(messages: list[dict]) -> list[str]:
    """从消息中提取文件路径（代码/文档引用）。"""
    found: list[str] = []
    seen: set[str] = set()
    for msg in messages:
        content = msg.get("content") or ""
        for m in _FILE_RE.finditer(content):
            path = m.group(0)
            if path not in seen:
                seen.add(path)
                found.append(path)
            if len(found) >= 6:
                return found
    return found


def _collect_recent(messages: list[dict], role: str, limit: int) -> list[str]:
    out: list[str] = []
    for msg in reversed(messages):
        if msg.get("role") == role and (msg.get("content") or "").strip():
            out.append(_truncate(msg["content"], 120))
            if len(out) >= limit:
                break
    out.reverse()
    return out


def _infer_pending(messages: list[dict]) -> list[str]:
    """从最近消息中提取含待办关键词的内容。"""
    out: list[str] = []
    for msg in reversed(messages):
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        lowered = content.lower()
        if any(kw in lowered for kw in _PENDING_KEYWORDS):
            out.append(_truncate(content, 120))
            if len(out) >= 3:
                break
    out.reverse()
    return out


def _infer_current_work(messages: list[dict]) -> str | None:
    """最后一条非空 assistant 消息作为当前进展。"""
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and (msg.get("content") or "").strip():
            return _truncate(msg["content"], 200)
    return None


def _infer_topic(messages: list[dict]) -> str:
    """从用户消息提炼主题关键词。"""
    user_texts = [m["content"] for m in messages if m.get("role") == "user" and m.get("content")]
    if not user_texts:
        return ""
    joined = " ".join(user_texts)
    # 中文词组 + 英文/数字 token
    tokens = re.findall(r"[一-鿿]{2,}|[A-Za-z0-9]{2,}", joined)
    stopwords = {"帮我", "看看", "一下", "这个", "那个", "怎么", "什么", "为什么", "可以", "吗", "呢", "吧", "的", "了"}
    keywords = [t for t in tokens if t.lower() not in stopwords and len(t) >= 2]
    seen: set[str] = set()
    uniq = [k for k in keywords if not (k in seen or seen.add(k))]
    return "、".join(uniq[:6])


def summarize_messages(messages: list[dict]) -> str:
    """把旧消息列表压成结构化 summary 文本（纯规则，不调 LLM）。"""
    n = len(messages)
    user_n = sum(1 for m in messages if m.get("role") == "user")
    assistant_n = sum(1 for m in messages if m.get("role") == "assistant")

    lines = [f"- 主题: {_infer_topic(messages) or '（未识别）'}"]
    lines.append(f"- 范围: 已压缩 {n} 条旧消息（用户 {user_n}，助手 {assistant_n}）。")

    recent_requests = _collect_recent(messages, "user", 3)
    if recent_requests:
        lines.append("- 近期用户请求:")
        lines.extend(f"  - {r}" for r in recent_requests)

    pending = _infer_pending(messages)
    if pending:
        lines.append("- 待办/未完成:")
        lines.extend(f"  - {p}" for p in pending)

    symbols = _extract_symbols(messages)
    files = _extract_files(messages)
    refs = symbols + files
    if refs:
        lines.append(f"- 涉及标的/文件: {'、'.join(refs[:10])}。")

    current = _infer_current_work(messages)
    if current:
        lines.append(f"- 当前进展: {current}")

    body = "\n".join(lines)
    summary = _SUMMARY_PREAMBLE + body
    if len(summary) > SUMMARY_MAX_CHARS:
        summary = summary[: SUMMARY_MAX_CHARS - 1] + "…"
    return summary


# ---------------------------------------------------------------------------
# 压缩主流程
# ---------------------------------------------------------------------------


def compact_history(messages: list[dict], *, preserve_recent: int = PRESERVE_RECENT,
                     max_tokens: int = MAX_ESTIMATED_TOKENS) -> tuple[list[dict], str]:
    """压缩对话历史。

    返回 (recent_messages, summary_text)：
      - 未触发压缩时，recent_messages = messages 原样，summary_text = ""
      - 触发压缩时，recent_messages = 最近 preserve_recent 条，summary_text = 旧消息的摘要
    summary 由调用方拼入 system prompt（复用 memory_context 注入通道），不作为 history 的 system 消息，避免破坏 run_agent_events 对 history role 的假设。
    """
    if not should_compact(messages, preserve_recent=preserve_recent, max_tokens=max_tokens):
        return messages, ""

    compactable = messages[:-preserve_recent] if len(messages) > preserve_recent else []
    recent = messages[-preserve_recent:] if len(messages) >= preserve_recent else messages[:]
    summary = summarize_messages(compactable)
    return recent, summary


# ---------------------------------------------------------------------------
# DB 接入：读历史 + 压缩
# ---------------------------------------------------------------------------


def _load_history_rows(db: Session, session_id: int, limit: int = HISTORY_LOAD_LIMIT) -> list[dict]:
    """从 DB 读最近 limit 条 user/assistant 消息（过滤 error），按时间正序。"""
    rows = db.scalars(
        select(AiChatMessage)
        .where(AiChatMessage.session_id == session_id, AiChatMessage.error.is_(None))
        .order_by(AiChatMessage.id.desc())
        .limit(limit)
    ).all()
    return [
        {"role": row.role, "content": row.content}
        for row in reversed(rows)
        if row.role in {"user", "assistant"} and row.content
    ]


def load_and_compact_history(db: Session, session_id: int) -> tuple[list[dict], str]:
    """读 DB 历史 + 压缩判断。返回 (history_for_llm, summary_text)。

    短对话（未超阈值）原样返回，summary_text 为空，行为与原 _messages_for_history 一致。
    """
    messages = _load_history_rows(db, session_id)
    return compact_history(messages)
