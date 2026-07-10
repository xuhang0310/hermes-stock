from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import (
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


@dataclass(frozen=True)
class CandidatePrompt:
    text: str
    reason: str
    source_type: str
    score: float


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
    context = _collect_context(db, user_id=user_id, digital_human_id=profile.id, chat_mode=chat_mode)
    candidates = _build_candidates(profile.code, context)
    if not candidates:
        candidates = _fallback_candidates(profile.code, context)

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


def refresh_all_recommendations(db: Session) -> int:
    users = db.scalars(select(User).where(User.is_enabled.is_(True))).all()
    profiles = db.scalars(
        select(DigitalHumanProfile).where(DigitalHumanProfile.status == "enabled").order_by(DigitalHumanProfile.sort_order, DigitalHumanProfile.id)
    ).all()
    count = 0
    for user in users:
        for profile in profiles:
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


def _collect_context(db: Session, *, user_id: int, digital_human_id: int, chat_mode: str) -> dict[str, Any]:
    since = datetime.now() - timedelta(days=30)
    messages = db.execute(
        select(AiChatMessage.content, AiChatMessage.created_at)
        .join(AiChatSession, AiChatSession.id == AiChatMessage.session_id)
        .where(
            AiChatSession.user_id == user_id,
            AiChatSession.digital_human_id == digital_human_id,
            AiChatSession.chat_mode == chat_mode,
            AiChatMessage.role == "user",
            AiChatMessage.created_at >= since,
        )
        .order_by(desc(AiChatMessage.id))
        .limit(40)
    ).all()
    message_texts = [content for content, _ in messages if content]
    symbols = _symbols_from_texts(message_texts)

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
    watch_rows = [{"symbol": item.symbol, "name": name or item.symbol, "group": group or "默认", "reason": item.reason} for item, name, group in watchlist]
    for row in position_rows + watch_rows:
        if row["symbol"] not in symbols:
            symbols.append(row["symbol"])

    return {
        "messages": message_texts,
        "symbols": symbols[:8],
        "positions": position_rows,
        "watchlist": watch_rows,
        "risks": [{"symbol": s, "level": l, "type": t, "finding": f} for s, l, t, f in risks],
        "events": [{"symbol": s, "level": l, "title": t} for s, l, t in events],
        "keyword_counts": _keyword_counts(message_texts),
    }


def _build_candidates(code: str, context: dict[str, Any]) -> list[CandidatePrompt]:
    builders = {
        "stock-pm": _pm_candidates,
        "risk-officer": _risk_candidates,
        "market-sentinel": _sentinel_candidates,
        "research-analyst": _research_candidates,
        "intelligence-officer": _intelligence_candidates,
        "quant-analyst": _quant_candidates,
        "macro-strategist": _macro_candidates,
        "short-term-hunter": _hunter_candidates,
    }
    return builders.get(code, _pm_candidates)(context)


def _fallback_candidates(code: str, context: dict[str, Any]) -> list[CandidatePrompt]:
    return {
        "stock-pm": [
            CandidatePrompt("今天有哪些需要优先处理的事件？", "总调度默认入口，适合先看全局优先级。", "role_default", 40),
            CandidatePrompt("把自选和持仓的重点列出来", "总调度需要先建立今日观察范围。", "role_default", 38),
        ],
        "risk-officer": [
            CandidatePrompt("当前持仓是否触发风控？", "风控官默认入口，先检查持仓风险。", "role_default", 40),
            CandidatePrompt("帮我反驳一下当前持仓逻辑", "风控官适合做反方论证。", "role_default", 36),
        ],
        "market-sentinel": [
            CandidatePrompt("最近的L0事件是什么？", "盘中哨兵默认关注事件优先级。", "role_default", 40),
            CandidatePrompt("哪些事件需要立即通知？", "盘中哨兵适合做通知判断。", "role_default", 36),
        ],
        "research-analyst": [
            CandidatePrompt("哪些标的缺少研究证据？", "研究员默认先找证据缺口。", "role_default", 40),
            CandidatePrompt("把报告结论整理成表格", "研究员适合整理已有证据。", "role_default", 36),
        ],
        "intelligence-officer": [
            CandidatePrompt("汇总最近影响持仓和自选池的情报", "情报官默认入口，先做消息过滤和来源分层。", "role_default", 40),
            CandidatePrompt("把外部消息按可信度排序并标出待核验项", "情报官需要先核验来源，再转交研究或风控。", "role_default", 36),
        ],
        "quant-analyst": [
            CandidatePrompt("比较持仓和自选池的量化信号", "量化分析师默认先做组合内横向比较。", "role_default", 40),
            CandidatePrompt("列出数据不足无法判断的标的", "量化分析师必须先说明样本和数据缺口。", "role_default", 36),
        ],
        "macro-strategist": [
            CandidatePrompt("当前宏观背景对持仓有什么影响？", "宏观策略师默认把宏观叙事落到持仓和自选池。", "role_default", 40),
            CandidatePrompt("列出需要跟踪的宏观验证指标", "宏观策略师需要把判断变成可验证指标。", "role_default", 36),
        ],
        "short-term-hunter": [
            CandidatePrompt("扫描自选和持仓里的短线候选", "短线猎手默认只在自选和持仓范围内扫描。", "role_default", 40),
            CandidatePrompt("列出需要冷静观察、不能追高的标的", "短线猎手必须优先提示纪律和风险。", "role_default", 36),
        ],
    }.get(code, [])


def _pm_candidates(context: dict[str, Any]) -> list[CandidatePrompt]:
    items = [
        CandidatePrompt("根据最近对话，生成今天的A股研究总调度清单", "你最近在数字人中心持续使用总调度入口，适合先汇总全局事项。", "history", 88),
        CandidatePrompt("把当前持仓和自选池按处理优先级排序", "系统已有持仓/自选池，适合总调度统一排优先级。", "position", 84),
    ]
    if context["events"]:
        items.append(CandidatePrompt("今天有哪些事件需要优先处理？", f"系统最近有 {len(context['events'])} 条事件记录。", "event", 82))
    if context["risks"]:
        items.append(CandidatePrompt("目前系统里最大的风险是什么？", f"系统最近有 {len(context['risks'])} 条风险记录。", "risk", 80))
    for symbol in context["symbols"][:2]:
        items.append(CandidatePrompt(f"围绕 {symbol} 生成下一步行动清单", "该标的出现在最近对话或当前持仓/自选池中。", "history", 76))
    if _mentioned(context, "日报"):
        items.append(CandidatePrompt("继续生成一份A股研究台日报，并突出未闭环事项", "你最近多次提到日报。", "history", 90))
    return items


def _risk_candidates(context: dict[str, Any]) -> list[CandidatePrompt]:
    items = [
        CandidatePrompt("检查当前持仓是否触发止损或仓位风险", "风控官会优先基于 open positions 做风险判断。", "position", 88),
        CandidatePrompt("列出当前持仓里缺少止损价或风险说明的标的", "持仓台账需要补齐风控字段。", "position", 84),
    ]
    for row in context["positions"][:3]:
        symbol = row["symbol"]
        if not row.get("stop_price"):
            items.append(CandidatePrompt(f"给 {symbol} 补一套止损和撤退条件", "该持仓缺少止损价。", "position", 90))
        items.append(CandidatePrompt(f"反驳一下 {symbol} 当前持仓逻辑", "风控官适合做反方论证。", "position", 78))
    if context["risks"]:
        items.append(CandidatePrompt("把最近风险记录按严重程度排序并给出处理建议", f"系统最近有 {len(context['risks'])} 条风险记录。", "risk", 86))
    if _mentioned(context, "止损") or _mentioned(context, "风险"):
        items.append(CandidatePrompt("复盘最近对话里提到的风险点，找出还没闭环的部分", "你最近关注风险/止损类问题。", "history", 92))
    return items


def _sentinel_candidates(context: dict[str, Any]) -> list[CandidatePrompt]:
    items = [
        CandidatePrompt("检查持仓和自选池里有没有需要通知的异动", "盘中哨兵默认关注通知价值。", "event", 88),
        CandidatePrompt("按标的汇总最近事件，并标记噪音和有效信号", "盘中哨兵适合做事件去重。", "event", 82),
    ]
    if context["events"]:
        items.append(CandidatePrompt("最近的L0/L1事件分别是什么？", f"系统最近有 {len(context['events'])} 条事件记录。", "event", 90))
    for symbol in context["symbols"][:3]:
        items.append(CandidatePrompt(f"检查 {symbol} 最近有没有异常事件或需要通知的信号", "该标的出现在最近对话或当前观察范围。", "history", 78))
    if _mentioned(context, "通知") or _mentioned(context, "异动"):
        items.append(CandidatePrompt("根据最近对话，判断哪些异动值得通知，哪些只是噪音", "你最近关注通知/异动判断。", "history", 92))
    return items


def _research_candidates(context: dict[str, Any]) -> list[CandidatePrompt]:
    items = [
        CandidatePrompt("识别当前持仓和自选池里缺少研究证据的标的", "研究员默认先检查证据缺口。", "research", 88),
        CandidatePrompt("把最近对话里提到的标的整理成研究优先级", "根据历史对话生成研究任务。", "history", 84),
    ]
    for symbol in context["symbols"][:4]:
        items.append(CandidatePrompt(f"给 {symbol} 生成一份研究备忘录", "该标的出现在最近对话或当前观察范围。", "history", 82))
        items.append(CandidatePrompt(f"梳理 {symbol} 的事件、报告和研究缺口", "研究员适合补齐个股证据链。", "research", 78))
    if _mentioned(context, "报告") or _mentioned(context, "研究"):
        items.append(CandidatePrompt("把最近讨论过的报告结论整理成表格", "你最近关注报告/研究整理。", "history", 90))
    return items


def _intelligence_candidates(context: dict[str, Any]) -> list[CandidatePrompt]:
    items = [
        CandidatePrompt("汇总最近影响持仓和自选池的情报", "情报官默认先做消息过滤和来源分层。", "event", 88),
        CandidatePrompt("把外部消息按可信度排序并标出待核验项", "情报官需要先核验来源，再转交研究或风控。", "history", 84),
    ]
    if context["events"]:
        items.append(CandidatePrompt("把最近事件按来源、级别和影响方向整理出来", f"系统最近有 {len(context['events'])} 条事件记录。", "event", 90))
    for symbol in context["symbols"][:3]:
        items.append(CandidatePrompt(f"检查 {symbol} 最近有没有公告、政策或舆情需要核验", "该标的出现在持仓/自选池或最近对话中。", "history", 80))
    if _mentioned(context, "通知") or _mentioned(context, "异动"):
        items.append(CandidatePrompt("把最近提到的异动消息分成有效情报和噪音", "你最近关注通知/异动判断。", "history", 88))
    return items


def _quant_candidates(context: dict[str, Any]) -> list[CandidatePrompt]:
    items = [
        CandidatePrompt("比较持仓和自选池的量化信号", "量化分析师默认先做组合内横向比较。", "position", 88),
        CandidatePrompt("列出数据不足无法判断的标的", "量化分析师必须先说明样本和数据缺口。", "research", 82),
    ]
    for symbol in context["symbols"][:4]:
        items.append(CandidatePrompt(f"给 {symbol} 生成量化信号快照", "该标的出现在持仓/自选池或最近对话中。", "history", 84))
    if context["positions"]:
        items.append(CandidatePrompt("按放量、回撤和相对强弱给当前持仓排序", "系统已有 open positions，可做横向因子比较。", "position", 86))
    return items


def _macro_candidates(context: dict[str, Any]) -> list[CandidatePrompt]:
    items = [
        CandidatePrompt("当前宏观背景对持仓有什么影响？", "宏观策略师默认把宏观叙事落到持仓和自选池。", "position", 88),
        CandidatePrompt("列出需要跟踪的宏观验证指标", "宏观策略师需要把判断变成可验证指标。", "role_default", 82),
    ]
    if context["watchlist"] or context["positions"]:
        items.append(CandidatePrompt("给自选池和持仓做一版宏观敏感性检查", "系统已有持仓/自选池，可映射宏观影响链。", "position", 86))
    if context["events"]:
        items.append(CandidatePrompt("把最近事件放到政策和行业轮动框架里解释", f"系统最近有 {len(context['events'])} 条事件记录。", "event", 80))
    for symbol in context["symbols"][:2]:
        items.append(CandidatePrompt(f"分析宏观和行业环境对 {symbol} 的影响", "该标的出现在当前观察范围内。", "history", 78))
    return items


def _hunter_candidates(context: dict[str, Any]) -> list[CandidatePrompt]:
    items = [
        CandidatePrompt("扫描自选和持仓里的短线候选", "短线猎手默认只在自选和持仓范围内扫描。", "position", 88),
        CandidatePrompt("列出需要冷静观察、不能追高的标的", "短线猎手必须优先提示纪律和风险。", "risk", 84),
    ]
    if context["events"]:
        items.append(CandidatePrompt("哪些事件可能形成短线情绪催化？", f"系统最近有 {len(context['events'])} 条事件记录。", "event", 86))
    for symbol in context["symbols"][:4]:
        items.append(CandidatePrompt(f"检查 {symbol} 是否有短线放量、异动或热点信号", "该标的出现在持仓/自选池或最近对话中。", "history", 82))
    if _mentioned(context, "异动"):
        items.append(CandidatePrompt("把最近异动标的按短线纪律重新排序", "你最近关注异动信号。", "history", 90))
    return items


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


def _keyword_counts(texts: list[str]) -> dict[str, int]:
    keywords = ["日报", "风险", "止损", "报告", "研究", "通知", "异动", "自选", "持仓", "政策", "公告", "宏观", "量化", "短线", "热点"]
    joined = "\n".join(texts)
    return {keyword: joined.count(keyword) for keyword in keywords}


def _mentioned(context: dict[str, Any], keyword: str) -> bool:
    return int(context.get("keyword_counts", {}).get(keyword, 0)) > 0
