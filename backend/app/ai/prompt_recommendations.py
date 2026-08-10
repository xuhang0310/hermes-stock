from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.ai.llm_client import chat_completion
from backend.app.core.llm_config import (
    active_llm_config,
    resolve_model_config_ids_by_priority,
)
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
from backend.app.core.settings import settings


SCHEDULER_INTERVAL_SECONDS = 30 * 60
RECOMMENDATION_TTL_HOURS = 10
FAILED_REFRESH_STALE_GRACE_HOURS = 2
MAX_RECOMMENDATIONS = 8
RECOMMENDATION_MODEL_PRIORITY = (
    "deepseek-v4-flash-0731",
    "deepseek-v4-pro",
)
RECOMMENDATION_MODEL_TIMEOUT_SECONDS = {
    "deepseek-v4-flash-0731": 18.0,
    "deepseek-v4-pro": 45.0,
}
RECOMMENDATION_FAILURE_BACKOFF_SECONDS = (300, 1800, 7200, 21600)
_scheduler_task: asyncio.Task | None = None
_refresh_tasks: set[asyncio.Task[None]] = set()
_refresh_claims: set[str] = set()
_refresh_claim_lock = threading.Lock()
_refresh_failure_state: dict[str, tuple[int, datetime]] = {}
_refresh_failure_lock = threading.Lock()

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
        "expires_at": _utc_iso(row.expires_at),
        "updated_at": _utc_iso(row.updated_at),
    }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc_iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def active_recommendations(
    db: Session,
    user_id: int,
    digital_human_id: int,
    limit: int = 6,
    *,
    chat_mode: str = "research",
) -> list[AiPromptRecommendation]:
    now = _utcnow()
    stale_cutoff = now - timedelta(hours=FAILED_REFRESH_STALE_GRACE_HOURS)
    return db.scalars(
        select(AiPromptRecommendation)
        .where(
            AiPromptRecommendation.user_id == user_id,
            AiPromptRecommendation.digital_human_id == digital_human_id,
            AiPromptRecommendation.chat_mode == chat_mode,
            AiPromptRecommendation.status == "active",
            (
                AiPromptRecommendation.expires_at.is_(None)
                | (AiPromptRecommendation.expires_at > now)
                | (
                    (AiPromptRecommendation.source_type != "fallback")
                    & (AiPromptRecommendation.expires_at > stale_cutoff)
                )
            ),
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
    return _refresh_recommendations(
        db,
        user_id=user_id,
        profile=profile,
        chat_mode=chat_mode,
        context_digital_human_id=profile.id,
    )


def _refresh_recommendations(
    db: Session,
    *,
    user_id: int,
    profile: DigitalHumanProfile,
    chat_mode: str,
    context_digital_human_id: int | None,
) -> list[AiPromptRecommendation]:
    now = _utcnow()
    existing = db.scalars(
        select(AiPromptRecommendation).where(
            AiPromptRecommendation.user_id == user_id,
            AiPromptRecommendation.digital_human_id == profile.id,
            AiPromptRecommendation.chat_mode == chat_mode,
            AiPromptRecommendation.status == "active",
        )
    ).all()
    context = _collect_context(
        db,
        user_id=user_id,
        digital_human_id=context_digital_human_id,
        chat_mode=chat_mode,
    )
    candidates = _build_candidates(
        profile,
        context,
        avoid_prompts=[row.prompt_text for row in existing],
        refresh_token=now.isoformat(timespec="seconds"),
    )
    generated_by_llm = bool(candidates)
    if not candidates:
        if context["messages"]:
            _record_refresh_failure(user_id, profile.id, chat_mode)
        if existing:
            # Stale-while-revalidate: a transient model/provider failure must
            # never replace a useful personalized batch with generic prompts.
            # The read path admits a bounded stale window without mutating the
            # old rows, so the scheduler can still see that a retry is needed.
            previous = active_recommendations(
                db,
                user_id,
                profile.id,
                chat_mode=chat_mode,
            )
            if previous:
                log_event(
                    "agent",
                    "WARNING",
                    f"推荐刷新未生成有效结果，保留上一批推荐：user={user_id}, profile={profile.code}",
                    to_db=False,
                )
                return previous
        candidates = _fallback_candidates(profile)

    expires_at = now + timedelta(hours=RECOMMENDATION_TTL_HOURS)
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
        if generated_by_llm:
            _clear_refresh_failure(user_id, profile.id, chat_mode)
        return active_recommendations(
            db,
            user_id,
            profile.id,
            chat_mode=chat_mode,
        )
    if generated_by_llm:
        _clear_refresh_failure(user_id, profile.id, chat_mode)
    return active_recommendations(
        db,
        user_id,
        profile.id,
        chat_mode=chat_mode,
    )


def schedule_recommendation_refresh(
    *,
    user_id: int,
    profile_id: int,
    chat_mode: str = "research",
) -> asyncio.Task[None]:
    """Refresh prompts off the response path so an SSE terminal event is never delayed by another LLM call."""
    task = asyncio.create_task(
        asyncio.to_thread(_refresh_one_in_new_session, user_id, profile_id, chat_mode)
    )
    _refresh_tasks.add(task)
    task.add_done_callback(_recommendation_refresh_done)
    return task


def _recommendation_refresh_done(task: asyncio.Task[None]) -> None:
    _refresh_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log_event(
            "agent",
            "ERROR",
            f"推荐问题后台任务异常退出: {str(exc)[:300]}",
            to_db=False,
        )


def _refresh_identity(user_id: int, profile_id: int, chat_mode: str) -> str:
    identity = f"{user_id}:{profile_id}:{chat_mode}"
    return hashlib.sha256(identity.encode()).hexdigest()


def _recommendation_redis_client():
    if not settings.agent_redis_url:
        return None
    try:
        from redis import Redis

        return Redis.from_url(
            settings.agent_redis_url,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
            decode_responses=True,
        )
    except Exception:  # noqa: BLE001
        return None


def _refresh_backoff_active(user_id: int, profile_id: int, chat_mode: str) -> bool:
    digest = _refresh_identity(user_id, profile_id, chat_mode)
    redis_client = _recommendation_redis_client()
    if redis_client is not None:
        try:
            return bool(redis_client.exists(f"hermes:prompt-refresh-cooldown:{digest}"))
        except Exception:  # noqa: BLE001
            pass
    with _refresh_failure_lock:
        state = _refresh_failure_state.get(digest)
        return bool(state and state[1] > _utcnow())


def _record_refresh_failure(user_id: int, profile_id: int, chat_mode: str) -> None:
    digest = _refresh_identity(user_id, profile_id, chat_mode)
    redis_client = _recommendation_redis_client()
    if redis_client is not None:
        try:
            failures_key = f"hermes:prompt-refresh-failures:{digest}"
            failure_count = int(redis_client.incr(failures_key))
            delay = RECOMMENDATION_FAILURE_BACKOFF_SECONDS[
                min(failure_count - 1, len(RECOMMENDATION_FAILURE_BACKOFF_SECONDS) - 1)
            ]
            redis_client.expire(failures_key, RECOMMENDATION_FAILURE_BACKOFF_SECONDS[-1] * 2)
            redis_client.set(f"hermes:prompt-refresh-cooldown:{digest}", "1", ex=delay)
            return
        except Exception:  # noqa: BLE001
            pass
    with _refresh_failure_lock:
        previous_count = _refresh_failure_state.get(digest, (0, _utcnow()))[0]
        failure_count = previous_count + 1
        delay = RECOMMENDATION_FAILURE_BACKOFF_SECONDS[
            min(failure_count - 1, len(RECOMMENDATION_FAILURE_BACKOFF_SECONDS) - 1)
        ]
        _refresh_failure_state[digest] = (
            failure_count,
            _utcnow() + timedelta(seconds=delay),
        )


def _clear_refresh_failure(user_id: int, profile_id: int, chat_mode: str) -> None:
    digest = _refresh_identity(user_id, profile_id, chat_mode)
    redis_client = _recommendation_redis_client()
    if redis_client is not None:
        with contextlib.suppress(Exception):
            redis_client.delete(
                f"hermes:prompt-refresh-failures:{digest}",
                f"hermes:prompt-refresh-cooldown:{digest}",
            )
    with _refresh_failure_lock:
        _refresh_failure_state.pop(digest, None)


@contextlib.contextmanager
def _recommendation_refresh_lease(user_id: int, profile_id: int, chat_mode: str):
    lock_key = f"hermes:prompt-refresh:{_refresh_identity(user_id, profile_id, chat_mode)}"
    token = uuid.uuid4().hex
    redis_client = _recommendation_redis_client()

    if redis_client is not None:
        try:
            if not redis_client.set(lock_key, token, nx=True, ex=90):
                yield False
                return
        except Exception:  # noqa: BLE001
            redis_client = None

    local_claimed = False
    if redis_client is None:
        with _refresh_claim_lock:
            if lock_key in _refresh_claims:
                yield False
                return
            _refresh_claims.add(lock_key)
            local_claimed = True

    try:
        yield True
    finally:
        if redis_client is not None:
            with contextlib.suppress(Exception):
                redis_client.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] "
                    "then return redis.call('del', KEYS[1]) else return 0 end",
                    1,
                    lock_key,
                    token,
                )
        elif local_claimed:
            with _refresh_claim_lock:
                _refresh_claims.discard(lock_key)


def refresh_recommendations_in_new_session(
    user_id: int,
    profile_id: int,
    chat_mode: str = "research",
) -> None:
    """Generate one recommendation batch outside the HTTP response path."""

    try:
        with _recommendation_refresh_lease(user_id, profile_id, chat_mode) as acquired:
            if not acquired:
                return
            with SessionLocal() as db:
                profile = db.get(DigitalHumanProfile, profile_id)
                if profile is None or profile.status != "enabled":
                    return
                refresh_recommendations_for_user_profile(
                    db,
                    user_id=user_id,
                    profile=profile,
                    chat_mode=chat_mode,
                )
    except Exception as exc:  # noqa: BLE001
        log_event(
            "agent",
            "WARNING",
            f"推荐问题后台刷新失败: {str(exc)[:200]}",
            to_db=False,
        )


def _refresh_one_in_new_session(user_id: int, profile_id: int, chat_mode: str) -> None:
    """Compatibility seam retained for scheduling tests and runtime patching."""

    refresh_recommendations_in_new_session(user_id, profile_id, chat_mode)


def refresh_recommendations_for_stock(db: Session, *, user_id: int) -> list[AiPromptRecommendation]:
    """为问股（stock 模式，会话不绑数字人）刷新推荐。

    复用问股虚拟 profile（code=ask_stock）作 persona / fallback / 存储锚点；会话历史按
    chat_mode=stock 过滤（不依赖 digital_human_id）。模式与 refresh_recommendations_for_user_profile
    逐字对齐。
    """
    profile = ensure_ask_stock_profile(db)
    return _refresh_recommendations(
        db,
        user_id=user_id,
        profile=profile,
        chat_mode="stock",
        context_digital_human_id=None,
    )


def refresh_all_recommendations(db: Session) -> int:
    """补偿刷新有新对话但推荐尚未更新的会话，避免全用户、全角色轮询 LLM。"""
    since = _utcnow() - timedelta(days=7)
    conversations = db.execute(
        select(
            AiChatSession.user_id,
            AiChatSession.digital_human_id,
            AiChatSession.chat_mode,
            func.max(AiChatMessage.created_at).label("last_message_at"),
        )
        .join(AiChatMessage, AiChatMessage.session_id == AiChatSession.id)
        .where(
            AiChatSession.status == "active",
            AiChatSession.chat_mode.in_(("research", "stock")),
            AiChatMessage.created_at >= since,
        )
        .group_by(
            AiChatSession.user_id,
            AiChatSession.digital_human_id,
            AiChatSession.chat_mode,
        )
    ).all()
    enabled_users = set(
        db.scalars(select(User.id).where(User.is_enabled.is_(True))).all()
    )
    ask_stock_profile = ensure_ask_stock_profile(db)
    count = 0
    for user_id, digital_human_id, chat_mode, last_message_at in conversations:
        if user_id not in enabled_users or last_message_at is None:
            continue
        if chat_mode == "stock":
            profile = ask_stock_profile
        elif digital_human_id:
            profile = db.get(DigitalHumanProfile, digital_human_id)
            if profile is None or profile.status != "enabled":
                continue
        else:
            continue

        if _refresh_backoff_active(user_id, profile.id, chat_mode):
            continue

        latest_recommendation = db.scalar(
            select(AiPromptRecommendation).where(
                AiPromptRecommendation.user_id == user_id,
                AiPromptRecommendation.digital_human_id == profile.id,
                AiPromptRecommendation.chat_mode == chat_mode,
                AiPromptRecommendation.status == "active",
            )
            .order_by(desc(AiPromptRecommendation.updated_at), desc(AiPromptRecommendation.id))
            .limit(1)
        )
        if (
            latest_recommendation
            and latest_recommendation.source_type != "fallback"
            and latest_recommendation.updated_at >= last_message_at
        ):
            continue
        if chat_mode == "stock":
            refresh_recommendations_for_stock(db, user_id=user_id)
        else:
            refresh_recommendations_for_user_profile(
                db,
                user_id=user_id,
                profile=profile,
                chat_mode=chat_mode,
            )
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
    if _scheduler_task:
        _scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _scheduler_task
        _scheduler_task = None
    pending = [task for task in _refresh_tasks if not task.done()]
    if not pending:
        return
    done, still_pending = await asyncio.wait(pending, timeout=30)
    for task in done:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()
    for task in still_pending:
        task.cancel()


async def _scheduler_loop() -> None:
    await asyncio.sleep(20)
    while True:
        try:
            await asyncio.to_thread(_refresh_all_in_new_session)
        except Exception as exc:  # noqa: BLE001
            log_event(
                "agent",
                "ERROR",
                f"推荐问题定时补偿刷新失败: {str(exc)[:300]}",
                to_db=False,
            )
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
    since = _utcnow() - timedelta(days=30)
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
        .where(
            Position.owner_user_id == user_id,
            Position.status == "open",
        )
        .order_by(desc(Position.position_pct), desc(Position.updated_at))
        .limit(20)
    ).all()
    watchlist = db.execute(
        select(WatchlistItem, Instrument.name, WatchlistGroup.name)
        .outerjoin(Instrument, Instrument.symbol == WatchlistItem.symbol)
        .outerjoin(WatchlistGroup, WatchlistGroup.id == WatchlistItem.group_id)
        .where(
            WatchlistItem.owner_user_id == user_id,
            WatchlistItem.status == "active",
        )
        .order_by(desc(WatchlistItem.updated_at))
        .limit(20)
    ).all()
    risks = db.execute(
        select(RiskCheck.symbol, RiskCheck.risk_level, RiskCheck.risk_type, RiskCheck.finding)
        .where(RiskCheck.owner_user_id == user_id)
        .order_by(desc(RiskCheck.created_at))
        .limit(20)
    ).all()
    events = db.execute(
        select(Event.symbol, Event.level, Event.title)
        .where(or_(Event.owner_user_id == user_id, Event.owner_user_id.is_(None)))
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
        "risks": [
            {"symbol": symbol, "level": level, "type": risk_type, "finding": finding}
            for symbol, level, risk_type, finding in risks
        ],
        "events": [
            {"symbol": symbol, "level": level, "title": title}
            for symbol, level, title in events
        ],
    }


# ---------------------------------------------------------------------------
# LLM 驱动的推荐生成（新核心逻辑）
# ---------------------------------------------------------------------------


def _build_candidates(
    profile: DigitalHumanProfile,
    context: dict[str, Any],
    *,
    avoid_prompts: list[str] | None = None,
    refresh_token: str | None = None,
) -> list[CandidatePrompt]:
    """使用 LLM 分析对话历史并生成推荐。无对话或 LLM 失败时返回空列表。"""
    if not context["messages"]:
        return []
    return _call_llm_for_recommendations(
        profile,
        context,
        avoid_prompts=avoid_prompts,
        refresh_token=refresh_token,
    )


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
    *,
    avoid_prompts: list[str] | None = None,
    refresh_token: str | None = None,
) -> list[CandidatePrompt]:
    """Generate recommendations with the dedicated model fallback chain."""
    model_config_ids = resolve_model_config_ids_by_priority(RECOMMENDATION_MODEL_PRIORITY)
    if not model_config_ids:
        log_event(
            "agent",
            "WARNING",
            "LLM 推荐跳过：未找到 deepseek-v4-flash-0731 或 deepseek-v4-pro 的可用配置",
            to_db=False,
        )
        return []

    messages = _build_llm_messages(
        profile,
        context,
        avoid_prompts=avoid_prompts,
        refresh_token=refresh_token,
    )
    errors: list[str] = []
    for attempt_index, model_config_id in enumerate(model_config_ids):
        model_label = str(model_config_id)
        try:
            llm_config = active_llm_config(model_config_id)
            model_label = str(llm_config.get("model") or model_config_id)
            if not llm_config.get("api_key") or not llm_config.get("base_url"):
                raise RuntimeError("未配置 API Key/Base URL")
            timeout_seconds = RECOMMENDATION_MODEL_TIMEOUT_SECONDS.get(model_label, 30.0)

            # 不覆盖模型自身温度，避免触发模型特有的 temperature 限制。
            data = chat_completion(
                messages,
                llm_config,
                temperature=None,
                timeout=timeout_seconds,
                log_module="prompt_recommendations",
            )
            content = data["choices"][0]["message"]["content"]
            candidates = _parse_llm_response(_extract_json(content))
            if not candidates:
                raise ValueError("模型未返回有效推荐")
            if attempt_index > 0:
                log_event(
                    "agent",
                    "INFO",
                    f"推荐模型已切换至兜底模型 {model_label}",
                    to_db=False,
                )
            return candidates
        except Exception as exc:  # noqa: BLE001
            error = f"{model_label}: {str(exc)[:240]}"
            errors.append(error)
            log_event(
                "agent",
                "WARNING",
                f"推荐模型 {model_label} 调用失败，尝试下一优先级模型: {str(exc)[:240]}",
                to_db=False,
            )

    log_event(
        "agent",
        "WARNING",
        f"LLM 推荐模型全部失败，将使用角色预设 fallback: {'; '.join(errors)[:500]}",
        to_db=False,
    )
    return []


def _build_llm_messages(
    profile: DigitalHumanProfile,
    context: dict[str, Any],
    *,
    avoid_prompts: list[str] | None = None,
    refresh_token: str | None = None,
) -> list[dict[str, str]]:
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

    previous_lines = [
        f"  - {text[:150]}"
        for text in (avoid_prompts or [])
        if text and text.strip()
    ]
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
        f"【上一批推荐，本次禁止原样重复】\n"
        f"{chr(10).join(previous_lines) if previous_lines else '（首次生成）'}\n"
        f"刷新批次：{refresh_token or 'initial'}\n"
        f"\n"
        f"本次必须结合最新对话重新选择角度，推荐文本不得与上一批原样相同。\n"
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

    # 从第一个可独立解码的 JSON 对象开始解析，避免贪婪正则吞掉多个对象。
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value

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
