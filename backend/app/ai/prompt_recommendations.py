from __future__ import annotations

import asyncio
import contextlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.llm_config import active_llm_config
from backend.app.core.applog import log_event
from backend.app.core.database import SessionLocal
from backend.app.core.models import (
    AiChatMessage,
    AiChatSession,
    AiPromptRecommendation,
    DigitalHumanProfile,
    Event,
    Instrument,
    Position,
    RiskCheck,
    User,
    WatchlistGroup,
    WatchlistItem,
)


SCHEDULER_INTERVAL_SECONDS = 30 * 60
RECOMMENDATION_TTL_HOURS = 10
MAX_RECOMMENDATIONS = 8
_scheduler_task: asyncio.Task | None = None

# 问股（stock 模式）虚拟 profile：问股会话不绑数字人（agent_chat stock 置 digital_human_id=None），
# 但 AiPromptRecommendation.digital_human_id NOT NULL，故用此虚拟 profile 作 persona / fallback /
# 推荐存储锚点。会话历史按 chat_mode=stock 过滤（不依赖 digital_human_id）。
ASK_STOCK_PROFILE_CODE = "ask_stock"
ASK_STOCK_QUICK_PROMPTS = [
    "帮我看看贵州茅台现在能不能买",
    "分析一下 300750 近期趋势和风险",
    "比亚迪最近有什么消息面变化",
    "600519 支撑位和压力位在哪里",
    "我想做短线，帮我筛一下这个票的风险点",
]
ASK_STOCK_SYSTEM_PROMPT = (
    "你是问股助手，基于 TuShare（日线/财务/资金流/龙虎榜/解禁等）与 Tavily 联网搜索，"
    "用 ReAct 方式回答 A 股研究问题：趋势/支撑压力/消息面/风险点。不提供投资建议。"
)


def ensure_ask_stock_profile(db: Session) -> DigitalHumanProfile:
    """启动时确保问股虚拟 profile 存在（code=ask_stock）。upsert by code：已存在不动。"""
    existing = db.scalar(
        select(DigitalHumanProfile).where(DigitalHumanProfile.code == ASK_STOCK_PROFILE_CODE).limit(1)
    )
    if existing is not None:
        return existing
    profile = DigitalHumanProfile(
        code=ASK_STOCK_PROFILE_CODE,
        name="问股助手",
        avatar_tone="analytical",
        target_roles="*",
        description="基于 TuShare/Tavily 的 A 股研究助手",
        system_prompt=ASK_STOCK_SYSTEM_PROMPT,
        quick_prompts_json=json.dumps(ASK_STOCK_QUICK_PROMPTS, ensure_ascii=False),
        status="enabled",
        sort_order=0,
    )
    db.add(profile)
    db.commit()
    return profile


@dataclass(frozen=True)
class CandidatePrompt:
    text: str
    reason: str
    source_type: str
    score: float


# ---------------------------------------------------------------------------
# 公共 API（保留不变）
# ---------------------------------------------------------------------------


def recommendation_out(row: AiPromptRecommendation) -> dict[str, Any]:
    return {
        "id": row.id,
        "digital_human_id": row.digital_human_id,
        "digital_human_code": row.digital_human_code,
        "chat_mode": row.chat_mode,
        "prompt_text": row.prompt_text,
        "reason": row.reason,
        "source_type": row.source_type,
        "score": row.score,
        "status": row.status,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def active_recommendations(db: Session, user_id: int, digital_human_id: int, limit: int = 6) -> list[AiPromptRecommendation]:
    now = datetime.now()
    return db.scalars(
        select(AiPromptRecommendation)
        .where(
            AiPromptRecommendation.user_id == user_id,
            AiPromptRecommendation.digital_human_id == digital_human_id,
            AiPromptRecommendation.status == "active",
            (AiPromptRecommendation.expires_at.is_(None) | (AiPromptRecommendation.expires_at > now)),
        )
        .order_by(desc(AiPromptRecommendation.score), desc(AiPromptRecommendation.updated_at), desc(AiPromptRecommendation.id))
        .limit(limit)
    ).all()


def refresh_recommendations_for_user_profile(
    db: Session,
    *,
    user_id: int,
    profile: DigitalHumanProfile,
    chat_mode: str = "research",
) -> list[AiPromptRecommendation]:
    """为一个用户+数字人组合刷新推荐（LLM 驱动，失败时走 fallback）。"""
    context = _collect_context(db, user_id=user_id, digital_human_id=profile.id, chat_mode=chat_mode)
    candidates = _build_candidates(profile, context)
    if not candidates:
        candidates = _fallback_candidates(profile)

    now = datetime.now()
    expires_at = now + timedelta(hours=RECOMMENDATION_TTL_HOURS)
    existing = db.scalars(
        select(AiPromptRecommendation).where(
            AiPromptRecommendation.user_id == user_id,
            AiPromptRecommendation.digital_human_id == profile.id,
            AiPromptRecommendation.status == "active",
        )
    ).all()
    for row in existing:
        row.status = "expired"

    rows: list[AiPromptRecommendation] = []
    for candidate in _dedupe_candidates(candidates)[:MAX_RECOMMENDATIONS]:
        row = AiPromptRecommendation(
            user_id=user_id,
            digital_human_id=profile.id,
            digital_human_code=profile.code,
            chat_mode=chat_mode,
            prompt_text=candidate.text[:500],
            reason=candidate.reason,
            source_type=candidate.source_type,
            score=candidate.score,
            status="active",
            expires_at=expires_at,
        )
        db.add(row)
        rows.append(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return active_recommendations(db, user_id, profile.id)
    return active_recommendations(db, user_id, profile.id)


def refresh_recommendations_for_stock(db: Session, *, user_id: int) -> list[AiPromptRecommendation]:
    """为问股（stock 模式，会话不绑数字人）刷新推荐。

    复用问股虚拟 profile（code=ask_stock）作 persona / fallback / 存储锚点；会话历史按
    chat_mode=stock 过滤（不依赖 digital_human_id）。模式与 refresh_recommendations_for_user_profile
    逐字对齐。
    """
    profile = ensure_ask_stock_profile(db)
    context = _collect_context(db, user_id=user_id, digital_human_id=None, chat_mode="stock")
    candidates = _build_candidates(profile, context)
    if not candidates:
        candidates = _fallback_candidates(profile)

    now = datetime.now()
    expires_at = now + timedelta(hours=RECOMMENDATION_TTL_HOURS)
    existing = db.scalars(
        select(AiPromptRecommendation).where(
            AiPromptRecommendation.user_id == user_id,
            AiPromptRecommendation.digital_human_id == profile.id,
            AiPromptRecommendation.chat_mode == "stock",
            AiPromptRecommendation.status == "active",
        )
    ).all()
    for row in existing:
        row.status = "expired"

    rows: list[AiPromptRecommendation] = []
    for candidate in _dedupe_candidates(candidates)[:MAX_RECOMMENDATIONS]:
        row = AiPromptRecommendation(
            user_id=user_id,
            digital_human_id=profile.id,
            digital_human_code=profile.code,
            chat_mode="stock",
            prompt_text=candidate.text[:500],
            reason=candidate.reason,
            source_type=candidate.source_type,
            score=candidate.score,
            status="active",
            expires_at=expires_at,
        )
        db.add(row)
        rows.append(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return active_recommendations(db, user_id, profile.id)
    return active_recommendations(db, user_id, profile.id)


def refresh_all_recommendations(db: Session) -> int:
    users = db.scalars(select(User).where(User.is_enabled.is_(True))).all()
    profiles = db.scalars(
        select(DigitalHumanProfile)
        .where(DigitalHumanProfile.status == "enabled")
        .order_by(DigitalHumanProfile.sort_order, DigitalHumanProfile.id)
    ).all()
    count = 0
    for user in users:
        for profile in profiles:
            if profile.code == ASK_STOCK_PROFILE_CODE:
                refresh_recommendations_for_stock(db, user_id=user.id)
            else:
                refresh_recommendations_for_user_profile(db, user_id=user.id, profile=profile)
            count += 1
    return count


def dismiss_recommendation(db: Session, *, user_id: int, recommendation_id: int) -> bool:
    row = db.get(AiPromptRecommendation, recommendation_id)
    if row is None or row.user_id != user_id:
        return False
    row.status = "dismissed"
    db.commit()
    return True


# ---------------------------------------------------------------------------
# 后台调度器（保留不变）
# ---------------------------------------------------------------------------


def start_prompt_recommendation_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        return
    _scheduler_task = asyncio.create_task(_scheduler_loop())


async def stop_prompt_recommendation_scheduler() -> None:
    global _scheduler_task
    if not _scheduler_task:
        return
    _scheduler_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _scheduler_task
    _scheduler_task = None


async def _scheduler_loop() -> None:
    await asyncio.sleep(20)
    while True:
        try:
            await asyncio.to_thread(_refresh_all_in_new_session)
        except Exception:
            pass
        await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)


def _refresh_all_in_new_session() -> None:
    with SessionLocal() as db:
        refresh_all_recommendations(db)


# ---------------------------------------------------------------------------
# 上下文收集（简化：去掉 keyword_counts，增加 assistant 消息）
# ---------------------------------------------------------------------------


def _collect_context(db: Session, *, user_id: int, digital_human_id: int | None, chat_mode: str) -> dict[str, Any]:
    """收集用户上下文：对话历史、持仓、自选、风险、事件。

    digital_human_id 为 None 时按 chat_mode 过滤会话（问股 stock 会话不绑 profile）。
    """
    since = datetime.now() - timedelta(days=30)
    session_filters = [
        AiChatSession.user_id == user_id,
        AiChatSession.chat_mode == chat_mode,
        AiChatMessage.created_at >= since,
    ]
    if digital_human_id is not None:
        session_filters.append(AiChatSession.digital_human_id == digital_human_id)
    messages = db.execute(
        select(AiChatMessage.role, AiChatMessage.content, AiChatMessage.created_at)
        .join(AiChatSession, AiChatSession.id == AiChatMessage.session_id)
        .where(*session_filters)
        .order_by(AiChatMessage.id.desc())
        .limit(60)
    ).all()

    message_dicts = [
        {"role": role, "content": content or ""}
        for role, content, _ in messages
        if content
    ]
    # 反向排列，让 LLM 按时间顺序阅读
    message_dicts.reverse()

    user_texts = [m["content"] for m in message_dicts if m["role"] == "user"]
    symbols = _symbols_from_texts(user_texts)

    positions = db.execute(
        select(Position, Instrument.name)
        .outerjoin(Instrument, Instrument.symbol == Position.symbol)
        .where(Position.status == "open")
        .order_by(desc(Position.position_pct), desc(Position.updated_at))
        .limit(20)
    ).all()
    watchlist = db.execute(
        select(WatchlistItem, Instrument.name, WatchlistGroup.name)
        .outerjoin(Instrument, Instrument.symbol == WatchlistItem.symbol)
        .outerjoin(WatchlistGroup, WatchlistGroup.id == WatchlistItem.group_id)
        .where(WatchlistItem.status == "active")
        .order_by(desc(WatchlistItem.updated_at))
        .limit(20)
    ).all()
    risks = db.execute(
        select(RiskCheck.symbol, RiskCheck.risk_level, RiskCheck.risk_type, RiskCheck.finding)
        .order_by(desc(RiskCheck.created_at))
        .limit(20)
    ).all()
    events = db.execute(
        select(Event.symbol, Event.level, Event.title)
        .order_by(desc(Event.created_at))
        .limit(20)
    ).all()

    position_rows = [
        {
            "symbol": item.symbol,
            "name": name or item.symbol,
            "position_pct": item.position_pct,
            "stop_price": item.stop_price,
            "risk_note": item.risk_note,
            "thesis": item.thesis,
        }
        for item, name in positions
    ]
    watch_rows = [
        {"symbol": item.symbol, "name": name or item.symbol, "group": group or "默认", "reason": item.reason}
        for item, name, group in watchlist
    ]
    for row in position_rows + watch_rows:
        if row["symbol"] not in symbols:
            symbols.append(row["symbol"])

    return {
        "messages": message_dicts,
        "symbols": symbols[:8],
        "positions": position_rows,
        "watchlist": watch_rows,
        "risks": [{"symbol": s, "level": l, "type": t, "finding": f} for s, l, t, f in risks],
        "events": [{"symbol": s, "level": l, "title": t} for s, l, t in events],
    }


# ---------------------------------------------------------------------------
# LLM 驱动的推荐生成（新核心逻辑）
# ---------------------------------------------------------------------------


def _build_candidates(profile: DigitalHumanProfile, context: dict[str, Any]) -> list[CandidatePrompt]:
    """使用 LLM 分析对话历史并生成推荐。无对话或 LLM 失败时返回空列表。"""
    if not context["messages"]:
        return []
    return _call_llm_for_recommendations(profile, context)


def _fallback_candidates(profile: DigitalHumanProfile) -> list[CandidatePrompt]:
    """LLM 生成失败时，从数字人的 quick_prompts_json 读取备用提示词。"""
    try:
        items = json.loads(profile.quick_prompts_json)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(items, list):
        return []
    result: list[CandidatePrompt] = []
    for item in items:
        if isinstance(item, str):
            result.append(CandidatePrompt(item, "角色预设提示词", "fallback", 30))
        elif isinstance(item, dict):
            result.append(CandidatePrompt(
                str(item.get("text", item.get("prompt", ""))),
                str(item.get("reason", "角色预设提示词")),
                "fallback",
                float(item.get("score", 30)),
            ))
    return [c for c in result if c.text]


def _call_llm_for_recommendations(
    profile: DigitalHumanProfile,
    context: dict[str, Any],
) -> list[CandidatePrompt]:
    """调用 LLM 分析对话并生成推荐。失败返回空列表。"""
    try:
        llm_config = active_llm_config()
        if not llm_config["api_key"] or not llm_config["base_url"]:
            log_event("agent", "WARNING", "LLM 推荐跳过：未配置 API Key/Base URL", to_db=False)
            return []

        messages = _build_llm_messages(profile, context)
        url = llm_config["base_url"].rstrip("/") + "/chat/completions"
        payload: dict[str, Any] = {
            "model": llm_config["model"],
            "messages": messages,
            "temperature": 0,
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {llm_config['api_key']}"}
        with httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
            response = client.post(url, json=payload, headers=headers)
            if response.status_code != 200:
                body = response.text[:300]
                raise RuntimeError(f"LLM 返回 {response.status_code}: {body}")

        data = response.json()
        content = data["choices"][0]["message"]["content"]
        parsed = _extract_json(content)
        return _parse_llm_response(parsed)
    except Exception:
        log_event("agent", "WARNING", "LLM 推荐生成失败，将使用 fallback", to_db=False)
        return []


def _build_llm_messages(profile: DigitalHumanProfile, context: dict[str, Any]) -> list[dict[str, str]]:
    """构建发送给 LLM 的消息（system + user）。"""
    system_prompt = (
        "你是一个用户意图分析专家，为股票研究平台的用户生成个性化后续提问推荐。\n"
        "\n"
        "你需要：\n"
        "1. 分析用户的历史对话，提取关注点、未解决问题、研究缺口\n"
        "2. 根据当前数字人角色的定位，生成 5-8 条最有价值的推荐提问\n"
        "3. 每条推荐的分数应反映其对用户的紧迫性和相关性（0-100，分数越高越紧急）\n"
        "\n"
        "输出严格的 JSON 格式，不要包含其他文字。"
    )

    # 最近的对话（最多 20 条）
    recent = context["messages"][-20:]
    convo_lines: list[str] = []
    for msg in recent:
        role_label = "用户" if msg["role"] == "user" else "助手"
        snippet = msg["content"][:500]
        convo_lines.append(f"[{role_label}] {snippet}")

    # 持仓
    pos_lines: list[str] = []
    for p in context["positions"]:
        parts = [f"{p['symbol']} {p['name']}", f"仓位{p['position_pct']}%"]
        if p.get("stop_price"):
            parts.append(f"止损{p['stop_price']}")
        if p.get("thesis"):
            parts.append(f"逻辑：{p['thesis'][:100]}")
        pos_lines.append("  " + " | ".join(parts))

    # 自选
    wl_lines: list[str] = []
    for w in context["watchlist"]:
        parts = [f"{w['symbol']} {w['name']}", f"分组：{w['group']}"]
        if w.get("reason"):
            parts.append(f"理由：{w['reason'][:80]}")
        wl_lines.append("  " + " | ".join(parts))

    # 事件
    ev_lines: list[str] = []
    for e in context["events"][:10]:
        ev_lines.append(f"  [{e['level']}] {e['symbol']} {e['title']}")

    user_message = (
        f"【当前角色】\n"
        f"名称：{profile.name}\n"
        f"定位：{profile.description}\n"
        f"角色说明：{profile.system_prompt[:800]}\n"
        f"\n"
        f"【用户最近对话】（共 {len(context['messages'])} 条消息，{'有历史对话' if convo_lines else '暂无对话记录'}）\n"
        f"{chr(10).join(convo_lines) if convo_lines else '（暂无对话记录）'}\n"
        f"\n"
        f"【用户当前持仓】\n"
        f"{chr(10).join(pos_lines) if pos_lines else '（暂无持仓）'}\n"
        f"\n"
        f"【用户自选池】\n"
        f"{chr(10).join(wl_lines) if wl_lines else '（暂无自选）'}\n"
        f"\n"
        f"【最近系统事件】\n"
        f"{chr(10).join(ev_lines) if ev_lines else '（暂无事件）'}\n"
        f"\n"
        f"请分析并生成推荐。只输出如下 JSON：\n"
        f'{{"analysis":{{"focus_symbols":["代码"],"topics":["主题"],"unresolved":["未解决问题"],"gaps":["研究缺口"]}},'
        f'"recommendations":[{{"text":"提问文本（不超过100字）","reason":"推荐理由","score":85,"source_type":"llm"}}]}}'
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]


def _extract_json(text: str) -> dict[str, Any]:
    """从 LLM 响应中安全提取 JSON，支持 code block 包裹和不完整截断。"""
    # 直接解析
    try:
        return json.loads(text)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        pass

    # 从 ```json ... ``` 中提取
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        try:
            return json.loads(m.group(1).strip())  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            pass

    # 从最外层 { } 中提取
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            pass

    raise ValueError(f"无法从 LLM 响应中提取 JSON，原始文本前 200 字符：{text[:200]}")


def _parse_llm_response(data: dict[str, Any]) -> list[CandidatePrompt]:
    """将 LLM 返回的 JSON 解析为 CandidatePrompt 列表。"""
    recommendations = data.get("recommendations")
    if not isinstance(recommendations, list):
        return []

    result: list[CandidatePrompt] = []
    for item in recommendations:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        result.append(CandidatePrompt(
            text=text[:500],
            reason=str(item.get("reason", ""))[:500],
            source_type=str(item.get("source_type", "llm")),
            score=min(100.0, max(0.0, float(item.get("score", 50)))),
        ))
    return result


# ---------------------------------------------------------------------------
# 去重 & 代码提取工具
# ---------------------------------------------------------------------------


def _dedupe_candidates(candidates: list[CandidatePrompt]) -> list[CandidatePrompt]:
    seen: set[str] = set()
    result: list[CandidatePrompt] = []
    for item in sorted(candidates, key=lambda row: row.score, reverse=True):
        key = re.sub(r"\s+", "", item.text)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _symbols_from_texts(texts: list[str]) -> list[str]:
    symbols: list[str] = []
    for text in texts:
        for raw in re.findall(r"\b(?:SH|SZ|BJ)?\d{6}(?:\.(?:SH|SZ|BJ))?\b", text.upper()):
            symbol = _normalize_symbol(raw)
            if symbol and symbol not in symbols:
                symbols.append(symbol)
    return symbols


def _normalize_symbol(raw: str) -> str | None:
    value = raw.strip().upper()
    match = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", value)
    if match:
        return f"{match.group(2)}.{match.group(1)}"
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value):
        return value
    if re.fullmatch(r"\d{6}", value):
        if value.startswith("6"):
            return f"{value}.SH"
        if value.startswith(("4", "8")):
            return f"{value}.BJ"
        return f"{value}.SZ"
    return None
