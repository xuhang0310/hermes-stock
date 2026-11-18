from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from backend.app.core.database import SessionLocal
from backend.app.core.models import (
    AutomaticSelectionCandidate,
    AutomaticSelectionEvaluation,
    AutomaticSelectionRun,
    AutomaticSelectionStockSnapshot,
    AutomaticSelectionStrategy,
    AutomaticSelectionStrategyEvent,
)
from backend.app.quant.automatic_selection_evaluation import EVALUATION_HORIZONS
from backend.app.quant.automatic_selection_scoring import (
    BUILTIN_STRATEGY_KEY,
    LAYERED_STRATEGY_CONFIG,
    LAYERED_STRATEGY_KEY,
    MODEL_RANK_STRATEGY_CONFIG,
    MODEL_RANK_STRATEGY_KEY,
    DEFAULT_STRATEGY_CONFIG,
    canonical_json,
    config_hash,
    required_tushare_factors,
    validate_config,
)

ACTIVE_STATES = ("official", "challenger", "shadow")
TERMINAL_RUN_STATUSES = ("completed", "blocked")
_STRATEGY_KEY_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")


def ensure_default_strategy() -> dict[str, Any]:
    with SessionLocal() as db:
        existing = db.scalar(
            select(AutomaticSelectionStrategy).where(
                AutomaticSelectionStrategy.strategy_key == BUILTIN_STRATEGY_KEY,
                AutomaticSelectionStrategy.version == 1,
            )
        )
        if existing is not None:
            return strategy_to_dict(existing)
        has_official = db.scalar(
            select(AutomaticSelectionStrategy.id).where(
                AutomaticSelectionStrategy.official_slot == 1
            ).limit(1)
        )
        state = "shadow" if has_official is not None else "official"
        row = AutomaticSelectionStrategy(
            strategy_key=BUILTIN_STRATEGY_KEY,
            version=1,
            name="前瞻多因子基准策略",
            lifecycle_state=state,
            official_slot=1 if state == "official" else None,
            enabled=True,
            config_json=canonical_json(DEFAULT_STRATEGY_CONFIG),
            config_hash=config_hash(DEFAULT_STRATEGY_CONFIG),
        )
        db.add(row)
        try:
            db.flush()
            db.add(
                AutomaticSelectionStrategyEvent(
                    strategy_id=row.id,
                    event_type="create",
                    to_state=state,
                    note="系统幂等创建内置 v1 策略",
                )
            )
            db.commit()
            db.refresh(row)
            return strategy_to_dict(row)
        except IntegrityError:
            db.rollback()
            existing = db.scalar(
                select(AutomaticSelectionStrategy).where(
                    AutomaticSelectionStrategy.strategy_key == BUILTIN_STRATEGY_KEY,
                    AutomaticSelectionStrategy.version == 1,
                )
            )
            if existing is None:
                raise
            return strategy_to_dict(existing)


def ensure_model_rank_strategy() -> dict[str, Any] | None:
    """幂等创建模型排名挑战者 v1（回测通过的做法，见 docs/quant-order-plan.md 第 4.10 节）。

    只建一次：之后被停用或晋升都按人的操作来，不会再自动建回来。
    """
    return _ensure_challenger(
        MODEL_RANK_STRATEGY_KEY,
        MODEL_RANK_STRATEGY_CONFIG,
        name="模型排名前 30（挑战者）",
        note="系统幂等创建模型排名挑战者 v1：回测两关通过，和正式策略并排观察，不替换",
    )


def ensure_layered_strategy() -> dict[str, Any] | None:
    """幂等创建分层打分挑战者 v1（量化选股 L1–L7 的波段总分，没有回测，见 docs/quant-selection.md）。

    和模型排名前 30 用同一套选股池和换仓规则，只为在前瞻评估里比成绩。同样只建一次。
    """
    return _ensure_challenger(
        LAYERED_STRATEGY_KEY,
        LAYERED_STRATEGY_CONFIG,
        name="分层打分前 30（挑战者，未验证）",
        note="系统幂等创建分层打分挑战者 v1：权重是人定的、没有回测，和模型排名前 30 用同一套规则并排记成绩",
    )


def _ensure_challenger(strategy_key: str, raw_config: dict[str, Any], *, name: str, note: str) -> dict[str, Any] | None:
    config = validate_config(raw_config)
    with SessionLocal() as db:
        existing = db.scalar(
            select(AutomaticSelectionStrategy).where(
                AutomaticSelectionStrategy.strategy_key == strategy_key,
                AutomaticSelectionStrategy.version == 1,
            )
        )
        if existing is not None:
            return strategy_to_dict(existing)
        row = AutomaticSelectionStrategy(
            strategy_key=strategy_key,
            version=1,
            name=name,
            lifecycle_state="challenger",
            enabled=True,
            config_json=canonical_json(config),
            config_hash=config_hash(config),
        )
        db.add(row)
        try:
            db.flush()
            db.add(
                AutomaticSelectionStrategyEvent(
                    strategy_id=row.id,
                    event_type="create",
                    to_state="challenger",
                    note=note,
                )
            )
            db.commit()
            db.refresh(row)
            return strategy_to_dict(row)
        except IntegrityError:
            db.rollback()
            existing = db.scalar(
                select(AutomaticSelectionStrategy).where(
                    AutomaticSelectionStrategy.strategy_key == strategy_key,
                    AutomaticSelectionStrategy.version == 1,
                )
            )
            return strategy_to_dict(existing) if existing is not None else None


def create_strategy(
    *,
    strategy_key: str,
    name: str,
    config: dict[str, Any],
    lifecycle_state: str,
    actor_user_id: int,
    parent_strategy_id: int | None = None,
) -> dict[str, Any]:
    key = str(strategy_key or "").strip().lower()
    if not _STRATEGY_KEY_RE.fullmatch(key):
        raise ValueError("策略标识只支持 3-64 位小写字母、数字和连字符，且必须以字母开头")
    display_name = str(name or "").strip()
    if not display_name:
        raise ValueError("策略名称不能为空")
    if lifecycle_state not in {"challenger", "shadow"}:
        raise ValueError("新策略只能创建为挑战者或影子策略，正式策略必须人工晋升")
    validated_config = validate_config(config)
    factor_ids = required_tushare_factors(validated_config)
    if factor_ids:
        from backend.app.quant.tushare_factor_service import ensure_catalog, known_factor_names

        ensure_catalog()
        known_names = known_factor_names()
        unknown_factors = sorted(
            factor_id
            for factor_id in factor_ids
            if factor_id.removeprefix("tushare:") not in known_names
        )
        if unknown_factors:
            raise ValueError(
                "TuShare 因子目录中不存在："
                + ", ".join(unknown_factors[:5])
                + "；请先同步因子目录"
            )
    payload = canonical_json(validated_config)
    with SessionLocal() as db:
        if parent_strategy_id is not None and db.get(AutomaticSelectionStrategy, parent_strategy_id) is None:
            raise ValueError("父策略不存在")
        latest_version = db.scalar(
            select(func.max(AutomaticSelectionStrategy.version)).where(
                AutomaticSelectionStrategy.strategy_key == key
            )
        )
        row = AutomaticSelectionStrategy(
            strategy_key=key,
            version=int(latest_version or 0) + 1,
            name=display_name[:160],
            lifecycle_state=lifecycle_state,
            enabled=True,
            parent_strategy_id=parent_strategy_id,
            config_json=payload,
            config_hash=config_hash(validated_config),
            created_by_user_id=actor_user_id,
        )
        db.add(row)
        try:
            db.flush()
            db.add(
                AutomaticSelectionStrategyEvent(
                    strategy_id=row.id,
                    event_type="create",
                    to_state=lifecycle_state,
                    actor_user_id=actor_user_id,
                    note="创建不可变策略版本",
                )
            )
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ValueError("策略版本并发创建冲突，请重试") from exc
        db.refresh(row)
        return strategy_to_dict(row)


def promote_strategy(strategy_id: int, *, actor_user_id: int, confirmation_note: str) -> dict[str, Any]:
    note = str(confirmation_note or "").strip()
    if len(note) < 4:
        raise ValueError("人工晋升确认说明至少 4 个字符")
    now = datetime.now()
    with SessionLocal() as db:
        target = db.scalar(
            select(AutomaticSelectionStrategy)
            .where(AutomaticSelectionStrategy.id == strategy_id)
            .with_for_update()
        )
        if target is None:
            raise ValueError("策略不存在")
        if target.lifecycle_state == "retired":
            raise ValueError("已退役策略不能晋升，请创建新版本")
        if target.official_slot == 1:
            return strategy_to_dict(target)
        evidence_run_id = _promotion_bootstrap_evidence(db, target)
        current = db.scalar(
            select(AutomaticSelectionStrategy)
            .where(AutomaticSelectionStrategy.official_slot == 1)
            .with_for_update()
        )
        if current is not None:
            previous_state = current.lifecycle_state
            current.official_slot = None
            current.lifecycle_state = "challenger"
            db.add(
                AutomaticSelectionStrategyEvent(
                    strategy_id=current.id,
                    event_type="demote",
                    from_state=previous_state,
                    to_state="challenger",
                    actor_user_id=actor_user_id,
                    note=f"由策略 {target.strategy_key} v{target.version} 替换",
                )
            )
            db.flush()
        previous_target_state = target.lifecycle_state
        target.lifecycle_state = "official"
        target.official_slot = 1
        target.enabled = True
        target.promoted_by_user_id = actor_user_id
        target.promotion_note = note
        target.promoted_at = now
        db.add(
            AutomaticSelectionStrategyEvent(
                strategy_id=target.id,
                event_type="promote",
                from_state=previous_target_state,
                to_state="official",
                actor_user_id=actor_user_id,
                note=(
                    f"{note}；最低启动证据：运行 {evidence_run_id} 的 5 日评估"
                    "（不代表统计验证，仍须人工判断）"
                ),
            )
        )
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ValueError("正式策略已被其他请求更新，请刷新后重试") from exc
        db.refresh(target)
        return strategy_to_dict(target)


def _not_superseded_run():
    replacement = aliased(AutomaticSelectionRun)
    return ~select(replacement.run_id).where(
        replacement.supersedes_run_id == AutomaticSelectionRun.run_id,
        replacement.strategy_id == AutomaticSelectionRun.strategy_id,
        replacement.target_trade_date == AutomaticSelectionRun.target_trade_date,
    ).correlate(AutomaticSelectionRun).exists()


def _latest_run_order() -> tuple:
    # MySQL timestamps have second precision; the primary key breaks ties.
    return (
        AutomaticSelectionRun.target_trade_date.desc(),
        AutomaticSelectionRun.created_at.desc(),
        AutomaticSelectionRun.run_id.desc(),
    )


def _promotion_bootstrap_evidence(db: Session, target: AutomaticSelectionStrategy) -> str:
    """One covered 5-day window is a bootstrap floor, not statistical validation."""
    from backend.app.quant.automatic_selection_scoring import SCORING_IMPLEMENTATION_VERSION

    config = validate_config(_loads(target.config_json, {}))
    min_coverage = max(
        DEFAULT_STRATEGY_CONFIG["quality"]["min_evaluation_coverage"],
        config["quality"]["min_evaluation_coverage"],
    )
    evidence = db.execute(
        select(
            AutomaticSelectionRun.run_id,
            AutomaticSelectionRun.quality_gate_json,
            AutomaticSelectionRun.sync_summary_json,
        )
        .join(
            AutomaticSelectionEvaluation,
            AutomaticSelectionEvaluation.run_id == AutomaticSelectionRun.run_id,
        )
        .where(
            AutomaticSelectionRun.strategy_id == target.id,
            AutomaticSelectionRun.config_hash == target.config_hash,
            AutomaticSelectionRun.status == "completed",
            AutomaticSelectionRun.selected_count > 0,
            AutomaticSelectionRun.eligible_count > 0,
            _not_superseded_run(),
            AutomaticSelectionEvaluation.horizon == min(EVALUATION_HORIZONS),
            AutomaticSelectionEvaluation.status == "completed",
            AutomaticSelectionEvaluation.entry_trade_date > AutomaticSelectionRun.target_trade_date,
            AutomaticSelectionEvaluation.exit_trade_date > AutomaticSelectionEvaluation.entry_trade_date,
            AutomaticSelectionEvaluation.selected_count == AutomaticSelectionRun.selected_count,
            AutomaticSelectionEvaluation.benchmark_count == AutomaticSelectionRun.eligible_count,
            AutomaticSelectionEvaluation.covered_count <= AutomaticSelectionEvaluation.selected_count,
            AutomaticSelectionEvaluation.benchmark_covered_count <= AutomaticSelectionEvaluation.benchmark_count,
            AutomaticSelectionEvaluation.covered_count
            >= AutomaticSelectionEvaluation.selected_count * min_coverage,
            AutomaticSelectionEvaluation.benchmark_covered_count
            >= AutomaticSelectionEvaluation.benchmark_count * min_coverage,
            AutomaticSelectionEvaluation.coverage_ratio.between(min_coverage, 1.0),
            AutomaticSelectionEvaluation.benchmark_coverage_ratio.between(min_coverage, 1.0),
        )
        .order_by(*_latest_run_order())
    ).mappings()
    for row in evidence:
        quality_gate = _loads(row["quality_gate_json"], {})
        sync_summary = _loads(row["sync_summary_json"], {})
        if (
            isinstance(quality_gate, dict)
            and quality_gate.get("passed") is True
            and isinstance(sync_summary, dict)
            and sync_summary.get("scoring_implementation_version") == SCORING_IMPLEMENTATION_VERSION
        ):
            return str(row["run_id"])
    raise ValueError(
        "尚未满足人工晋升的最低启动证据门槛：至少需要 1 次同配置、同评分实现版本、"
        "质量门通过且未被替代的已完成运行，并完成 5 日前瞻评估；"
        f"入选组合及合格池基准覆盖率均须达到 {min_coverage:.0%}。"
        "此门槛不代表统计验证，无需等待全部 20/60 日窗口。"
    )


def retire_strategy(strategy_id: int, *, actor_user_id: int, note: str | None = None) -> dict[str, Any]:
    with SessionLocal() as db:
        row = db.scalar(
            select(AutomaticSelectionStrategy)
            .where(AutomaticSelectionStrategy.id == strategy_id)
            .with_for_update()
        )
        if row is None:
            raise ValueError("策略不存在")
        if row.official_slot == 1:
            raise ValueError("正式策略不能直接退役，请先人工晋升替代策略")
        if row.lifecycle_state == "retired":
            return strategy_to_dict(row)
        previous_state = row.lifecycle_state
        row.lifecycle_state = "retired"
        row.enabled = False
        row.retired_at = datetime.now()
        db.add(
            AutomaticSelectionStrategyEvent(
                strategy_id=row.id,
                event_type="retire",
                from_state=previous_state,
                to_state="retired",
                actor_user_id=actor_user_id,
                note=str(note or "").strip() or None,
            )
        )
        db.commit()
        db.refresh(row)
        return strategy_to_dict(row)


def list_strategies(*, include_retired: bool = True) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        stmt = select(AutomaticSelectionStrategy)
        if not include_retired:
            stmt = stmt.where(AutomaticSelectionStrategy.lifecycle_state != "retired")
        rows = db.scalars(
            stmt.order_by(
                AutomaticSelectionStrategy.official_slot.desc(),
                AutomaticSelectionStrategy.strategy_key,
                AutomaticSelectionStrategy.version.desc(),
            )
        ).all()
        return [strategy_to_dict(row) for row in rows]


def active_strategy_models() -> list[AutomaticSelectionStrategy]:
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(AutomaticSelectionStrategy)
                .where(
                    AutomaticSelectionStrategy.enabled.is_(True),
                    AutomaticSelectionStrategy.lifecycle_state.in_(ACTIVE_STATES),
                )
                .order_by(
                    AutomaticSelectionStrategy.official_slot.desc(),
                    AutomaticSelectionStrategy.id,
                )
            ).all()
        )


def get_strategy_model(strategy_id: int) -> AutomaticSelectionStrategy | None:
    with SessionLocal() as db:
        return db.get(AutomaticSelectionStrategy, strategy_id)


def upsert_stock_snapshots(observed_trade_date: str, rows: Iterable[dict[str, Any]]) -> int:
    now = datetime.now()
    count = 0
    with SessionLocal() as db:
        # The first committed population owns the whole observation date. A
        # rerun must not append newly visible symbols to its frozen universe.
        existing = db.scalar(
            select(AutomaticSelectionStockSnapshot.id)
            .where(AutomaticSelectionStockSnapshot.observed_trade_date == observed_trade_date)
            .limit(1)
            .with_for_update()
        )
        if existing is not None:
            return 0
        seen: set[str] = set()
        for payload in rows:
            symbol = str(payload.get("symbol") or "")
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            values = {
                "name": str(payload.get("name") or symbol)[:120],
                "market": _nullable_text(payload.get("market"), 24),
                "stock_type": str(payload.get("stock_type") or "stock")[:24],
                "list_status": str(payload.get("list_status") or "L")[:8],
                "list_date": _nullable_text(payload.get("list_date"), 8),
                "industry": _nullable_text(payload.get("industry"), 120),
                "source": str(payload.get("source") or "tushare.stock_basic")[:40],
                "raw_json": canonical_json(payload),
                "fetched_at": now,
            }
            db.add(
                AutomaticSelectionStockSnapshot(
                    observed_trade_date=observed_trade_date,
                    symbol=symbol,
                    **values,
                )
            )
            count += 1
        db.commit()
    return count


def latest_stock_snapshots(observed_trade_date: str) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        rows = db.scalars(
            select(AutomaticSelectionStockSnapshot)
            .where(AutomaticSelectionStockSnapshot.observed_trade_date == observed_trade_date)
            .order_by(AutomaticSelectionStockSnapshot.symbol)
        ).all()
        return [
            {
                "symbol": row.symbol,
                "name": row.name,
                "market": row.market,
                "stock_type": row.stock_type,
                "list_status": row.list_status,
                "list_date": row.list_date,
                "industry": row.industry,
                "source": row.source,
                "fetched_at": _iso(row.fetched_at),
            }
            for row in rows
        ]


def find_run_by_inputs(
    strategy_id: int, target_trade_date: str, strategy_config_hash: str, fingerprint: str,
) -> dict[str, Any] | None:
    with SessionLocal() as db:
        row = db.scalar(
            select(AutomaticSelectionRun).where(
                AutomaticSelectionRun.strategy_id == strategy_id,
                AutomaticSelectionRun.target_trade_date == target_trade_date,
                AutomaticSelectionRun.config_hash == strategy_config_hash,
                AutomaticSelectionRun.data_fingerprint == fingerprint,
            )
        )
        return run_to_dict(row) if row is not None else None


def latest_run_model(
    strategy_id: int,
    *,
    before_trade_date: str | None = None,
    completed_only: bool = False,
) -> AutomaticSelectionRun | None:
    with SessionLocal() as db:
        stmt = select(AutomaticSelectionRun).where(
            AutomaticSelectionRun.strategy_id == strategy_id,
            _not_superseded_run(),
        )
        if before_trade_date:
            stmt = stmt.where(AutomaticSelectionRun.target_trade_date < before_trade_date)
        if completed_only:
            stmt = stmt.where(AutomaticSelectionRun.status == "completed")
        return db.scalar(
            stmt.order_by(*_latest_run_order()).limit(1)
        )


def latest_run_for_target_model(
    strategy_id: int, target_trade_date: str,
) -> AutomaticSelectionRun | None:
    with SessionLocal() as db:
        return db.scalar(
            select(AutomaticSelectionRun)
            .where(
                AutomaticSelectionRun.strategy_id == strategy_id,
                AutomaticSelectionRun.target_trade_date == target_trade_date,
                _not_superseded_run(),
            )
            .order_by(*_latest_run_order())
            .limit(1)
        )


def selected_symbols(run_id: str | None) -> list[str]:
    if not run_id:
        return []
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(AutomaticSelectionCandidate.symbol)
                .where(
                    AutomaticSelectionCandidate.run_id == run_id,
                    AutomaticSelectionCandidate.selected.is_(True),
                )
                .order_by(AutomaticSelectionCandidate.symbol)
            ).all()
        )


def selected_candidate_snapshots(
    run_id: str | None,
) -> list[dict[str, Any]]:
    if not run_id:
        return []
    with SessionLocal() as db:
        rows = db.execute(
            select(
                AutomaticSelectionCandidate.symbol,
                AutomaticSelectionCandidate.name,
                AutomaticSelectionCandidate.market,
                AutomaticSelectionCandidate.list_status,
                AutomaticSelectionCandidate.list_date,
                AutomaticSelectionCandidate.industry,
                AutomaticSelectionCandidate.market_cap_bucket,
            )
            .where(
                AutomaticSelectionCandidate.run_id == run_id,
                AutomaticSelectionCandidate.selected.is_(True),
            )
            .order_by(AutomaticSelectionCandidate.symbol)
        ).all()
        return [
            {
                "symbol": str(row.symbol),
                "name": row.name,
                "market": row.market,
                "list_status": row.list_status,
                "list_date": row.list_date,
                "industry": row.industry,
                "market_cap_bucket": row.market_cap_bucket,
            }
            for row in rows
        ]


def persist_run(
    *,
    run_payload: dict[str, Any],
    candidates: Iterable[dict[str, Any]],
    create_evaluations: bool,
) -> tuple[dict[str, Any], bool]:
    run_id = str(run_payload.get("run_id") or uuid.uuid4().hex)
    values = {key: value for key, value in run_payload.items() if key != "run_id"}
    values.setdefault("started_at", datetime.now())
    with SessionLocal() as db:
        row = AutomaticSelectionRun(run_id=run_id, **values)
        db.add(row)
        for item in candidates:
            db.add(_candidate_model(run_id, item))
        if create_evaluations:
            for horizon in EVALUATION_HORIZONS:
                db.add(
                    AutomaticSelectionEvaluation(
                        run_id=run_id,
                        horizon=horizon,
                        status="pending",
                        selected_count=int(run_payload.get("selected_count") or 0),
                        turnover=run_payload.get("selected_turnover"),
                    )
                )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.scalar(
                select(AutomaticSelectionRun).where(
                    AutomaticSelectionRun.strategy_id == run_payload["strategy_id"],
                    AutomaticSelectionRun.target_trade_date == run_payload["target_trade_date"],
                    AutomaticSelectionRun.config_hash == run_payload["config_hash"],
                    AutomaticSelectionRun.data_fingerprint == run_payload["data_fingerprint"],
                )
            )
            if existing is None:
                raise
            return run_to_dict(existing), False
        db.refresh(row)
        return run_to_dict(row), True


def update_run_explanation(
    run_id: str,
    *,
    status: str,
    model: str | None = None,
    explanation: str | None = None,
    error: str | None = None,
) -> None:
    with SessionLocal() as db:
        row = db.get(AutomaticSelectionRun, run_id)
        if row is None:
            return
        row.llm_status = status
        row.llm_model = model
        row.llm_explanation = explanation
        row.llm_error = error
        db.commit()


def list_runs(
    *, strategy_id: int | None = None, status: str | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        stmt = select(AutomaticSelectionRun)
        if strategy_id is not None:
            stmt = stmt.where(AutomaticSelectionRun.strategy_id == strategy_id)
        if status:
            stmt = stmt.where(AutomaticSelectionRun.status == status)
        rows = db.scalars(
            stmt.order_by(*_latest_run_order()).limit(max(1, min(int(limit), 200)))
        ).all()
        return [run_to_dict(row) for row in rows]


def run_detail(
    run_id: str,
    *,
    candidate_scope: str = "top",
    candidate_limit: int = 200,
    candidate_offset: int = 0,
) -> dict[str, Any] | None:
    limit = max(1, min(int(candidate_limit), 200))
    offset = max(0, int(candidate_offset))
    with SessionLocal() as db:
        run = db.get(AutomaticSelectionRun, run_id)
        if run is None:
            return None
        conditions = [AutomaticSelectionCandidate.run_id == run_id]
        if candidate_scope == "top":
            conditions.append(
                AutomaticSelectionCandidate.selected.is_(True)
            )
        elif candidate_scope == "eligible":
            conditions.append(
                AutomaticSelectionCandidate.eligible.is_(True)
            )
        elif candidate_scope == "excluded":
            conditions.append(
                AutomaticSelectionCandidate.eligible.is_(False)
            )
        elif candidate_scope != "all":
            raise ValueError("候选范围只支持 top/eligible/excluded/all")
        candidate_total = int(
            db.scalar(
                select(func.count())
                .select_from(AutomaticSelectionCandidate)
                .where(*conditions)
            )
            or 0
        )
        candidates = db.scalars(
            select(AutomaticSelectionCandidate)
            .where(*conditions)
            .order_by(
                AutomaticSelectionCandidate.selected.desc(),
                AutomaticSelectionCandidate.rank,
                AutomaticSelectionCandidate.symbol,
            )
            .limit(limit)
            .offset(offset)
        ).all()
        evaluations = db.scalars(
            select(AutomaticSelectionEvaluation)
            .where(AutomaticSelectionEvaluation.run_id == run_id)
            .order_by(AutomaticSelectionEvaluation.horizon)
        ).all()
        data = run_to_dict(run)
        data["candidates"] = [
            candidate_to_dict(row)
            for row in candidates
        ]
        data["candidate_total"] = candidate_total
        data["candidate_limit"] = limit
        data["candidate_offset"] = offset
        data["evaluations"] = [
            evaluation_to_dict(row)
            for row in evaluations
        ]
        return data


def run_evaluations(
    run_id: str,
) -> list[dict[str, Any]] | None:
    with SessionLocal() as db:
        if db.get(AutomaticSelectionRun, run_id) is None:
            return None
        rows = db.scalars(
            select(AutomaticSelectionEvaluation)
            .where(AutomaticSelectionEvaluation.run_id == run_id)
            .order_by(AutomaticSelectionEvaluation.horizon)
        ).all()
        return [evaluation_to_dict(row) for row in rows]


def pending_evaluation_contexts(*, limit: int = 100) -> list[dict[str, Any]]:
    cap = max(1, min(int(limit), 500))
    with SessionLocal() as db:
        stmt = (
            select(
                AutomaticSelectionEvaluation.id.label("evaluation_id"),
                AutomaticSelectionEvaluation.run_id,
                AutomaticSelectionEvaluation.horizon,
                AutomaticSelectionEvaluation.turnover,
                AutomaticSelectionEvaluation.updated_at,
                AutomaticSelectionRun.target_trade_date,
                AutomaticSelectionRun.config_json,
            )
            .join(
                AutomaticSelectionRun,
                AutomaticSelectionRun.run_id == AutomaticSelectionEvaluation.run_id,
            )
            .where(
                AutomaticSelectionRun.status == "completed",
                _not_superseded_run(),
            )
            .order_by(
                AutomaticSelectionEvaluation.updated_at,
                AutomaticSelectionEvaluation.id,
            )
            .limit(cap)
        )
        # Reserve half the batch for each queue; neither old retries nor still
        # immature pending windows may monopolize it. Unused quota is reusable.
        pending = list(db.execute(
            stmt.where(AutomaticSelectionEvaluation.status == "pending")
        ).mappings())
        retries = list(db.execute(
            stmt.where(AutomaticSelectionEvaluation.status == "insufficient_data")
        ).mappings())
        quota = cap // 2
        rows = pending[:quota] + retries[:quota]
        remaining = sorted(
            pending[quota:] + retries[quota:],
            key=lambda row: (row["updated_at"], row["evaluation_id"]),
        )
        # For cap=1 choose the least recently attempted head across both queues.
        rows.extend(remaining[:cap - len(rows)])
        rows.sort(key=lambda row: (row["updated_at"], row["evaluation_id"]))
        if not rows:
            return []
        run_ids = sorted({str(row["run_id"]) for row in rows})
        symbols_by_run: dict[str, dict[str, set[str]]] = {
            run_id: {"selected": set(), "benchmark": set()}
            for run_id in run_ids
        }
        candidate_rows = db.execute(
            select(
                AutomaticSelectionCandidate.run_id,
                AutomaticSelectionCandidate.symbol,
                AutomaticSelectionCandidate.selected,
                AutomaticSelectionCandidate.eligible,
            ).where(
                AutomaticSelectionCandidate.run_id.in_(run_ids),
                or_(
                    AutomaticSelectionCandidate.selected.is_(True),
                    AutomaticSelectionCandidate.eligible.is_(True),
                ),
            )
        ).all()
        for run_id, symbol, selected, eligible in candidate_rows:
            values = symbols_by_run[str(run_id)]
            if selected:
                values["selected"].add(str(symbol))
            if eligible:
                values["benchmark"].add(str(symbol))
        return [
            {
                "evaluation_id": int(row["evaluation_id"]),
                "run_id": str(row["run_id"]),
                "target_trade_date": str(row["target_trade_date"]),
                "horizon": int(row["horizon"]),
                "turnover": row["turnover"],
                "config": _loads(str(row["config_json"]), {}),
                "selected_symbols": sorted(symbols_by_run[str(row["run_id"])]["selected"]),
                "benchmark_symbols": sorted(symbols_by_run[str(row["run_id"])]["benchmark"]),
            }
            for row in rows
        ]


def update_evaluation_result(evaluation_id: int, result: dict[str, Any]) -> dict[str, Any] | None:
    scalar_keys = {
        "status",
        "entry_trade_date",
        "exit_trade_date",
        "selected_count",
        "covered_count",
        "coverage_ratio",
        "benchmark_count",
        "benchmark_covered_count",
        "benchmark_coverage_ratio",
        "basket_return",
        "benchmark_return",
        "excess_return",
        "max_drawdown",
        "sharpe",
        "information_ratio",
        "turnover",
        "commission_rate",
        "slippage_rate",
        "limitations",
        "error",
    }
    payload = {key: value for key, value in result.items() if key in scalar_keys}
    payload["daily_series_json"] = canonical_json(result.get("daily_series") or [])
    payload["per_symbol_json"] = canonical_json(result.get("per_symbol") or [])
    if result.get("status") != "pending":
        payload["evaluated_at"] = datetime.now()
    return update_evaluation(evaluation_id, payload)


def evaluation_models(statuses: tuple[str, ...] = ("pending",)) -> list[AutomaticSelectionEvaluation]:
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(AutomaticSelectionEvaluation)
                .join(
                    AutomaticSelectionRun,
                    AutomaticSelectionRun.run_id == AutomaticSelectionEvaluation.run_id,
                )
                .where(
                    AutomaticSelectionEvaluation.status.in_(statuses),
                    AutomaticSelectionRun.status == "completed",
                    _not_superseded_run(),
                )
                .order_by(AutomaticSelectionEvaluation.updated_at, AutomaticSelectionEvaluation.id)
            ).all()
        )


def update_evaluation(evaluation_id: int, payload: dict[str, Any]) -> dict[str, Any] | None:
    with SessionLocal() as db:
        row = db.get(AutomaticSelectionEvaluation, evaluation_id)
        if row is None:
            return None
        for key, value in payload.items():
            if hasattr(row, key):
                setattr(row, key, value)
        # An unchanged pending/retry result still consumed a queue attempt.
        # ORM onupdate alone does not fire when every business field is equal.
        row.updated_at = datetime.now()
        db.commit()
        db.refresh(row)
        return evaluation_to_dict(row)


def load_run_and_candidate_models(
    run_id: str,
) -> tuple[AutomaticSelectionRun | None, list[AutomaticSelectionCandidate]]:
    with SessionLocal() as db:
        run = db.get(AutomaticSelectionRun, run_id)
        candidates = list(
            db.scalars(
                select(AutomaticSelectionCandidate)
                .where(AutomaticSelectionCandidate.run_id == run_id)
                .order_by(AutomaticSelectionCandidate.rank, AutomaticSelectionCandidate.symbol)
            ).all()
        )
        return run, candidates


def strategy_to_dict(row: AutomaticSelectionStrategy) -> dict[str, Any]:
    return {
        "id": row.id,
        "strategy_key": row.strategy_key,
        "version": row.version,
        "name": row.name,
        "lifecycle_state": row.lifecycle_state,
        "enabled": row.enabled,
        "is_official": row.official_slot == 1,
        "parent_strategy_id": row.parent_strategy_id,
        "config": _loads(row.config_json, {}),
        "config_hash": row.config_hash,
        "created_by_user_id": row.created_by_user_id,
        "promoted_by_user_id": row.promoted_by_user_id,
        "promotion_note": row.promotion_note,
        "promoted_at": _iso(row.promoted_at),
        "retired_at": _iso(row.retired_at),
        "created_at": _iso(row.created_at),
    }


def run_to_dict(row: AutomaticSelectionRun) -> dict[str, Any]:
    return {
        "run_id": row.run_id,
        "strategy_id": row.strategy_id,
        "strategy_key": row.strategy_key,
        "strategy_version": row.strategy_version,
        "strategy_name": row.strategy_name,
        "target_trade_date": row.target_trade_date,
        "trigger_type": row.trigger_type,
        "job_run_id": row.job_run_id,
        "status": row.status,
        "stage": row.stage,
        "config": _loads(row.config_json, {}),
        "config_hash": row.config_hash,
        "data_fingerprint": row.data_fingerprint,
        "supersedes_run_id": row.supersedes_run_id,
        "sync_summary": _loads(row.sync_summary_json, {}),
        "quality_gate": _loads(row.quality_gate_json, {}),
        "exclusion_summary": _loads(row.exclusion_summary_json, {}),
        "universe_count": row.universe_count,
        "eligible_count": row.eligible_count,
        "selected_count": row.selected_count,
        "selected_turnover": row.selected_turnover,
        "llm_status": row.llm_status,
        "llm_model": row.llm_model,
        "llm_explanation": row.llm_explanation,
        "llm_error": row.llm_error,
        "error": row.error,
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "created_at": _iso(row.created_at),
    }


def candidate_to_dict(row: AutomaticSelectionCandidate) -> dict[str, Any]:
    return {
        "symbol": row.symbol,
        "name": row.name,
        "market": row.market,
        "list_status": row.list_status,
        "list_date": row.list_date,
        "industry": row.industry,
        "market_cap_bucket": row.market_cap_bucket,
        "eligible": row.eligible,
        "exclusion_codes": _loads(row.exclusion_codes_json, []),
        "constraint_codes": _loads(row.constraint_codes_json, []),
        "close_price": row.close_price,
        "pct_chg": row.pct_chg,
        "pe_ttm": row.pe_ttm,
        "pb": row.pb,
        "total_mv": row.total_mv,
        "circ_mv": row.circ_mv,
        "avg_amount_20": row.avg_amount_20,
        "momentum_20": row.momentum_20,
        "volatility_20": row.volatility_20,
        "input_evidence": _loads(row.input_evidence_json, {}),
        "factor_values": _loads(row.factor_values_json, {}),
        "z_scores": _loads(row.z_scores_json, {}),
        "composite_score": row.composite_score,
        "rank": row.rank,
        "percentile": row.percentile,
        "selected": row.selected,
        "research_weight": row.research_weight,
        "change_state": row.change_state,
    }


def evaluation_to_dict(row: AutomaticSelectionEvaluation) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "horizon": row.horizon,
        "status": row.status,
        "entry_trade_date": row.entry_trade_date,
        "exit_trade_date": row.exit_trade_date,
        "selected_count": row.selected_count,
        "covered_count": row.covered_count,
        "coverage_ratio": row.coverage_ratio,
        "benchmark_count": row.benchmark_count,
        "benchmark_covered_count": row.benchmark_covered_count,
        "benchmark_coverage_ratio": row.benchmark_coverage_ratio,
        "basket_return": row.basket_return,
        "benchmark_return": row.benchmark_return,
        "excess_return": row.excess_return,
        "max_drawdown": row.max_drawdown,
        "sharpe": row.sharpe,
        "information_ratio": row.information_ratio,
        "turnover": row.turnover,
        "commission_rate": row.commission_rate,
        "slippage_rate": row.slippage_rate,
        "daily_series": _loads(row.daily_series_json, []),
        "per_symbol": _loads(row.per_symbol_json, []),
        "limitations": row.limitations,
        "error": row.error,
        "evaluated_at": _iso(row.evaluated_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _candidate_model(run_id: str, item: dict[str, Any]) -> AutomaticSelectionCandidate:
    return AutomaticSelectionCandidate(
        run_id=run_id,
        symbol=str(item.get("symbol") or "")[:24],
        name=str(item.get("name") or item.get("symbol") or "")[:120],
        market=_nullable_text(item.get("market"), 24),
        list_status=_nullable_text(item.get("list_status"), 8),
        list_date=_nullable_text(item.get("list_date"), 8),
        industry=_nullable_text(item.get("industry"), 120),
        market_cap_bucket=_nullable_text(item.get("market_cap_bucket"), 16),
        eligible=bool(item.get("eligible")),
        exclusion_codes_json=canonical_json(item.get("exclusion_codes") or []),
        constraint_codes_json=canonical_json(item.get("constraint_codes") or []),
        close_price=item.get("close_price"),
        pct_chg=item.get("pct_chg"),
        pe_ttm=item.get("pe_ttm"),
        pb=item.get("pb"),
        total_mv=item.get("total_mv"),
        circ_mv=item.get("circ_mv"),
        avg_amount_20=item.get("avg_amount_20"),
        momentum_20=item.get("momentum_20"),
        volatility_20=item.get("volatility_20"),
        input_evidence_json=canonical_json(item.get("input_evidence") or {}),
        factor_values_json=canonical_json(item.get("factor_values") or {}),
        z_scores_json=canonical_json(item.get("z_scores") or {}),
        composite_score=item.get("composite_score"),
        rank=item.get("rank"),
        percentile=item.get("percentile"),
        selected=bool(item.get("selected")),
        research_weight=item.get("research_weight"),
        change_state=str(item.get("change_state") or "unchanged")[:24],
    )


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _nullable_text(value: Any, limit: int) -> str | None:
    text = str(value or "").strip()
    return text[:limit] if text else None


# ---- 白话呈现层（automatic_selection_presentation）使用的只读查询 ----


def display_strategy(strategy_id: int | None = None) -> dict[str, Any] | None:
    """普通用户默认看模型排名方案（回测通过的那个），它还没出过结果或被停用时看正式策略；都没有时退回最近一次运行所属的策略。

    只决定页面和每日摘要默认展示谁，不动 official_slot：正式策略的晋升记录和评估照旧。
    """
    with SessionLocal() as db:
        if strategy_id is not None:
            row = db.get(AutomaticSelectionStrategy, strategy_id)
            return strategy_to_dict(row) if row is not None else None
        row = db.scalar(
            select(AutomaticSelectionStrategy)
            .where(
                AutomaticSelectionStrategy.strategy_key == MODEL_RANK_STRATEGY_KEY,
                AutomaticSelectionStrategy.enabled.is_(True),
                AutomaticSelectionStrategy.lifecycle_state != "retired",
                select(AutomaticSelectionRun.run_id)
                .where(
                    AutomaticSelectionRun.strategy_id == AutomaticSelectionStrategy.id,
                    AutomaticSelectionRun.status == "completed",
                )
                .exists(),
            )
            .order_by(AutomaticSelectionStrategy.version.desc())
            .limit(1)
        )
        if row is None:
            row = db.scalar(
                select(AutomaticSelectionStrategy).where(AutomaticSelectionStrategy.official_slot == 1)
            )
        if row is None:
            latest = db.scalar(
                select(AutomaticSelectionRun.strategy_id).order_by(*_latest_run_order()).limit(1)
            )
            row = db.get(AutomaticSelectionStrategy, latest) if latest is not None else None
        return strategy_to_dict(row) if row is not None else None


def strategy_run_index(strategy_id: int, *, limit: int = 60) -> list[dict[str, Any]]:
    """某策略每个目标交易日的最终运行（已排除被修订替代的运行），按日期倒序。"""
    with SessionLocal() as db:
        rows = db.execute(
            select(
                AutomaticSelectionRun.run_id,
                AutomaticSelectionRun.target_trade_date,
                AutomaticSelectionRun.status,
            )
            .where(AutomaticSelectionRun.strategy_id == strategy_id, _not_superseded_run())
            .order_by(*_latest_run_order())
            .limit(max(1, min(int(limit), 250)))
        ).all()
    seen: set[str] = set()
    index: list[dict[str, Any]] = []
    for run_id, trade_date, status in rows:
        if trade_date in seen:
            continue
        seen.add(trade_date)
        index.append({"run_id": run_id, "trade_date": trade_date, "status": status})
    return index


def eligible_score_pool(run_id: str) -> list[dict[str, Any]]:
    """合格池每只股票的 z 值，用于把单只股票换算成“超过了多少合格股票”。"""
    with SessionLocal() as db:
        rows = db.execute(
            select(AutomaticSelectionCandidate.symbol, AutomaticSelectionCandidate.z_scores_json).where(
                AutomaticSelectionCandidate.run_id == run_id,
                AutomaticSelectionCandidate.eligible.is_(True),
            )
        ).all()
    return [{"symbol": str(symbol), "z_scores": _loads(z_scores, {})} for symbol, z_scores in rows]


def removed_candidates(run_id: str) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        rows = db.execute(
            select(AutomaticSelectionCandidate.symbol, AutomaticSelectionCandidate.name)
            .where(
                AutomaticSelectionCandidate.run_id == run_id,
                AutomaticSelectionCandidate.change_state == "removed",
            )
            .order_by(AutomaticSelectionCandidate.symbol)
        ).all()
    return [{"symbol": str(symbol), "name": name or str(symbol)} for symbol, name in rows]


def constraint_skipped_count(run_id: str) -> int:
    """排名够前但因行业/市值上限没能入选的股票数。"""
    with SessionLocal() as db:
        return int(
            db.scalar(
                select(func.count())
                .select_from(AutomaticSelectionCandidate)
                .where(
                    AutomaticSelectionCandidate.run_id == run_id,
                    AutomaticSelectionCandidate.eligible.is_(True),
                    AutomaticSelectionCandidate.selected.is_(False),
                    AutomaticSelectionCandidate.constraint_codes_json != "[]",
                )
            )
            or 0
        )


def track_record_rows(strategy_id: int, *, limit: int = 60) -> list[dict[str, Any]]:
    """已完成运行及其 5/20/60 日评估，按目标交易日倒序。"""
    with SessionLocal() as db:
        runs = db.scalars(
            select(AutomaticSelectionRun)
            .where(
                AutomaticSelectionRun.strategy_id == strategy_id,
                AutomaticSelectionRun.status == "completed",
                _not_superseded_run(),
            )
            .order_by(*_latest_run_order())
            .limit(max(1, min(int(limit), 250)))
        ).all()
        run_ids = [row.run_id for row in runs]
        evaluations = (
            db.scalars(
                select(AutomaticSelectionEvaluation).where(AutomaticSelectionEvaluation.run_id.in_(run_ids))
            ).all()
            if run_ids
            else []
        )
        by_run: dict[str, dict[int, dict[str, Any]]] = {}
        for row in evaluations:
            by_run.setdefault(row.run_id, {})[int(row.horizon)] = {
                "status": row.status,
                "entry_trade_date": row.entry_trade_date,
                "exit_trade_date": row.exit_trade_date,
                "basket_return": row.basket_return,
                "benchmark_return": row.benchmark_return,
                "excess_return": row.excess_return,
                "max_drawdown": row.max_drawdown,
                "coverage_ratio": row.coverage_ratio,
            }
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in runs:
            if row.target_trade_date in seen:
                continue
            seen.add(row.target_trade_date)
            result.append(
                {
                    "run_id": row.run_id,
                    "target_trade_date": row.target_trade_date,
                    "selected_count": row.selected_count,
                    "eligible_count": row.eligible_count,
                    "evaluations": by_run.get(row.run_id, {}),
                }
            )
        return result
