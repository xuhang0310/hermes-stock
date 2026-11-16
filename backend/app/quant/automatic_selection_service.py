from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterable, Iterator

from sqlalchemy import func, select, text

from backend.app.ai.llm_client import chat_completion
from backend.app.core.database import SessionLocal
from backend.app.core.llm_config import active_llm_config, reasoning_params
from backend.app.core.models import DailyBar, StockDailyBasic
from backend.app.market.market_sync import (
    load_local_adj_factors,
    load_local_history_trade_dates,
    load_local_open_trade_dates,
    load_local_stock_catalog_snapshot,
    local_selection_cache_status,
    selection_target_ceiling,
    sync_market,
)
from backend.app.quant import automatic_selection_repository as repository
from backend.app.quant.automatic_selection_evaluation import calculate_turnover
from backend.app.quant.automatic_selection_presentation import explanation_evidence
from backend.app.quant.automatic_selection_scoring import (
    MODEL_RANK_FEATURE,
    RESEARCH_LIMITATIONS,
    SCORING_IMPLEMENTATION_VERSION,
    apply_membership_changes,
    build_candidates,
    canonical_json,
    config_hash,
    data_fingerprint,
    exclusion_summary,
    fingerprint_payload,
    quality_gate,
    rebalance_state,
    required_tushare_factors,
    score_candidates,
    uses_model_rank,
    validate_config,
)
from backend.app.quant.tushare_factor_service import (
    MAX_SYNC_FACTORS,
    ensure_catalog,
    known_factor_names,
    load_factor_panel,
    parse_tushare_factor_id,
    sync_factor_values,
)

logger = logging.getLogger(__name__)
_LOCK_NAME = "hermes_stock_automatic_selection"


def _chunks(values: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _date_variants(dates: Iterable[str]) -> list[str]:
    values: set[str] = set()
    for value in dates:
        compact = str(value).replace("-", "")
        if len(compact) == 8:
            values.update((compact, f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"))
    return sorted(values)


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def sync_automatic_selection_data() -> dict[str, Any]:
    with _ingestion_lock():
        repository.ensure_default_strategy()
        repository.ensure_model_rank_strategy()
        strategies = [
            repository.strategy_to_dict(row)
            for row in repository.active_strategy_models()
        ]
        # 同一轮入库与校验共用一个目标上限，跨过 17:00 也不会前后口径不一。
        ceiling = selection_target_ceiling()
        market_summary = sync_market(days=60, target_ceiling=ceiling)
        cache_status = local_selection_cache_status(target_ceiling=ceiling)
        target_trade_date = str(cache_status.get("expected_trade_date") or "")
        factor_ids, config_errors = _collect_factor_requirements(
            strategies
        )
        factor_sync = (
            _sync_required_factors(factor_ids, target_trade_date)
            if target_trade_date
            else {
                "requested": factor_ids,
                "stored_rows": 0,
                "errors": ["本地交易日历尚无可用目标交易日"],
                "batches": [],
            }
        )
        market_errors = [
            *[str(item) for item in market_summary.get("errors") or []],
            *[str(item) for item in cache_status.get("errors") or []],
        ]
        if not cache_status.get("ready") and not market_errors:
            market_errors.append("本地行情缓存未通过完整性检查")
        errors = [
            *market_errors,
            *config_errors,
            *[str(item) for item in factor_sync.get("errors") or []],
        ]
        return {
            "ready": not market_errors,
            "target_trade_date": target_trade_date or None,
            "strategy_count": len(strategies),
            "cache_status": cache_status,
            "market_sync": market_summary,
            "factor_sync": factor_sync,
            "errors": errors,
        }


def run_automatic_selection_cycle(
    *, trigger_type: str = "schedule", job_run_id: str | None = None,
) -> dict[str, Any]:
    evaluation_summary = update_due_evaluations()
    with _selection_lock():
        repository.ensure_default_strategy()
        repository.ensure_model_rank_strategy()
        strategies = [repository.strategy_to_dict(row) for row in repository.active_strategy_models()]
        if not strategies:
            raise RuntimeError("没有启用的正式、挑战者或影子策略")
        cache_status: dict[str, Any] = {}
        target_trade_date = ""
        summary: dict[str, Any] = {}
        try:
            cache_status = local_selection_cache_status()
            # 空目标仅记录失败，不能把请求日或陈旧缓存日冒充可排名的交易日。
            target_trade_date = str(cache_status.get("expected_trade_date") or "")
            summary["cache_status"] = cache_status
            if not cache_status.get("ready") or not target_trade_date:
                raise RuntimeError("自动选股本地缓存未就绪：" + "；".join(
                    str(item) for item in cache_status.get("errors") or ["缺少可信目标交易日"]
                ))
            summary["ingestion"] = _completed_ingestion_evidence(target_trade_date)
            catalog = repository.latest_stock_snapshots(target_trade_date)
            snapshot_count = 0
            if not catalog:
                current_catalog = [
                    item for item in load_local_stock_catalog_snapshot()
                    if _is_supported_a_share(item.get("symbol"))
                ]
                if not current_catalog:
                    raise RuntimeError("本地 A 股目录为空，无法执行自动选股")
                snapshot_count = repository.upsert_stock_snapshots(target_trade_date, current_catalog)
                catalog = repository.latest_stock_snapshots(target_trade_date)
            if not catalog:
                raise RuntimeError("本日不可变股票目录快照为空，无法执行自动选股")
            factor_ids, config_errors = _collect_factor_requirements(strategies)
            factor_values = _load_target_factor_values(factor_ids, target_trade_date)
            if _any_uses_model_rank(strategies):
                # 没算出来就是空字典，用模型排名的策略会被质量门禁拦下，其他策略照常
                factor_values[MODEL_RANK_FEATURE] = _load_model_ranks(target_trade_date)
            rows = load_candidate_input_rows(
                catalog,
                target_trade_date=target_trade_date,
                history_days=20,
                expected_history_dates=cache_status.get("expected_history_dates") or [],
                factor_values=factor_values,
            )
            summary.update({
                "snapshot_rows_inserted": snapshot_count,
                "catalog_count": len(catalog),
                "factor_cache": {
                    "requested": factor_ids + ([MODEL_RANK_FEATURE] if MODEL_RANK_FEATURE in factor_values else []),
                    "symbols_by_factor": {key: len(values) for key, values in factor_values.items()},
                    "configuration_errors": config_errors,
                },
            })
            result = run_strategies_from_rows(
                strategies,
                rows,
                target_trade_date=target_trade_date,
                trigger_type=trigger_type,
                job_run_id=job_run_id,
                sync_summary=summary,
                defer_explanation=True,
            )
        except Exception as exc:
            logger.warning("自动选股输入准备失败 target_trade_date=%s error=%s", target_trade_date, exc)
            runs = [
                _persist_failed_strategy_run(
                    strategy,
                    target_trade_date=target_trade_date,
                    trigger_type=trigger_type,
                    job_run_id=job_run_id,
                    sync_summary=summary,
                    error=exc,
                )
                for strategy in strategies
            ]
            result = {
                "target_trade_date": target_trade_date or None,
                "strategy_count": len(runs),
                "completed_count": 0,
                "blocked_count": 0,
                "failed_count": len(runs),
                "selected_count": 0,
                "runs": runs,
            }
    _explain_completed_runs(result)
    result["evaluation_updates"] = evaluation_summary
    result["market_sync"] = summary
    return result


def _completed_ingestion_evidence(target_trade_date: str) -> dict[str, Any]:
    from backend.app.scheduler.rhythm_scheduler import successful_automatic_selection_ingestion

    with SessionLocal() as db:
        latest = successful_automatic_selection_ingestion(db, target_trade_date)
        if latest is None:
            raise RuntimeError("目标交易日尚无成功的数据入库记录，请先完成数据入库任务")
        return {"job_run_id": latest.run_id, "target_trade_date": target_trade_date}


def _validated_strategy_config(
    strategy: dict[str, Any],
    errors: list[str],
) -> dict[str, Any] | None:
    try:
        return validate_config(dict(strategy.get("config") or {}))
    except Exception as exc:
        message = (
            f"策略 {strategy.get('strategy_key')}@{strategy.get('version')} "
            f"配置无效：{exc}"
        )
        if message not in errors:
            errors.append(message)
        return None


def _collect_factor_requirements(
    strategies: Iterable[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    factor_ids: set[str] = set()
    errors: list[str] = []
    for strategy in strategies:
        config = _validated_strategy_config(strategy, errors)
        if config is not None:
            factor_ids.update(required_tushare_factors(config))
    return sorted(factor_ids), errors


def _any_uses_model_rank(strategies: Iterable[dict[str, Any]]) -> bool:
    errors: list[str] = []
    return any(
        config is not None and uses_model_rank(config)
        for config in (_validated_strategy_config(strategy, errors) for strategy in strategies)
    )


def _load_model_ranks(target_trade_date: str) -> dict[str, float]:
    """读当天的模型排名（stock_model_signals）。影子账户 18:05 才算，选股先到时由这里算好存表，影子账户直接复用。"""
    from backend.app.shadow.rank_signal import latest_signals, load_signals, save_signals

    try:
        with SessionLocal() as db:
            saved = load_signals(db, target_trade_date)
            if saved is not None:
                return {symbol: float(rank) for symbol, rank in saved.ranks.items() if _finite_float(rank) is not None}
            signals = latest_signals(db, target_trade_date)
            if signals.complete:
                try:
                    save_signals(db, target_trade_date, signals)
                    db.commit()
                except Exception as exc:  # noqa: BLE001 - 影子任务同时存了也没关系，排名照用
                    db.rollback()
                    logger.warning("模型信号存表失败 trade_date=%s error=%s", target_trade_date, str(exc)[:200])
            return {symbol: float(rank) for symbol, rank in signals.ranks.items() if _finite_float(rank) is not None}
    except Exception as exc:  # noqa: BLE001 - 失败只影响用模型排名的策略
        logger.warning("模型排名读取失败 trade_date=%s error=%s", target_trade_date, str(exc)[:200])
        return {}


def _explain_completed_runs(result: dict[str, Any]) -> None:
    runs = result.get("runs") or []
    for index, run in enumerate(runs):
        if (
            run.get("status") != "completed"
            or run.get("llm_status") != "pending"
            or not run.get("run_id")
        ):
            continue
        run_id = str(run["run_id"])
        _generate_persisted_explanation(run_id)
        detail = _safe_run_detail(run_id)
        if detail is not None:
            runs[index] = {
                **detail,
                "created": bool(run.get("created")),
            }


def load_candidate_input_rows(
    catalog: Iterable[dict[str, Any]],
    *,
    target_trade_date: str,
    history_days: int,
    expected_history_dates: list[str] | None = None,
    factor_values: dict[str, dict[str, float]] | None = None,
) -> list[dict[str, Any]]:
    snapshots = sorted(
        (dict(item) for item in catalog),
        key=lambda item: str(item.get("symbol") or ""),
    )
    symbols = sorted(
        {
            str(item.get("symbol") or "")
            for item in snapshots
            if item.get("symbol")
        }
    )
    expected_dates = (
        list(expected_history_dates)
        if expected_history_dates is not None
        else load_local_history_trade_dates(end_inclusive=target_trade_date, limit=history_days + 1)
    )
    requested_dates = _date_variants([*expected_dates, target_trade_date])
    histories: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    basics: dict[str, dict[str, Any]] = {}
    normalized_bar_date = func.replace(DailyBar.trade_date, "-", "")
    with SessionLocal() as db:
        for symbol_batch in _chunks(symbols, 500):
            bar_rows = db.execute(
                select(
                    DailyBar.id,
                    DailyBar.symbol,
                    normalized_bar_date.label("trade_date"),
                    DailyBar.open,
                    DailyBar.close,
                    DailyBar.amount,
                    DailyBar.change_pct,
                )
                .where(
                    DailyBar.source == "tushare",
                    DailyBar.symbol.in_(symbol_batch),
                    DailyBar.trade_date.in_(requested_dates),
                )
                .order_by(DailyBar.id)
            ).all()
            for _, symbol, trade_date, open_price, close_price, amount, change_pct in bar_rows:
                symbol_key = str(symbol)
                date_key = str(trade_date)
                histories[symbol_key][date_key] = {
                    "trade_date": date_key,
                    "open": open_price,
                    "close": close_price,
                    "amount": amount,
                    "change_pct": change_pct,
                }
            basic_rows = db.execute(
                select(
                    StockDailyBasic.id,
                    StockDailyBasic.symbol,
                    StockDailyBasic.pe_ttm,
                    StockDailyBasic.pb,
                    StockDailyBasic.total_mv,
                    StockDailyBasic.circ_mv,
                )
                .where(
                    StockDailyBasic.symbol.in_(symbol_batch),
                    StockDailyBasic.trade_date.in_(_date_variants([target_trade_date])),
                )
                .order_by(StockDailyBasic.id)
            ).all()
            for _, symbol, pe_ttm, pb, total_mv, circ_mv in basic_rows:
                basics[str(symbol)] = {
                    "pe_ttm": pe_ttm,
                    "pb": pb,
                    "total_mv": total_mv,
                    "circ_mv": circ_mv,
                }
        adj_factors = load_local_adj_factors(db, symbols, [*expected_dates, target_trade_date])
    for symbol_key, bars in histories.items():
        for date_key, bar in bars.items():
            bar["adj_factor"] = adj_factors.get((symbol_key, date_key))

    supplied = factor_values or {}
    result: list[dict[str, Any]] = []
    required_history_bars = history_days + 1
    for snapshot in snapshots:
        symbol = str(snapshot.get("symbol") or "")
        history = sorted(
            histories.get(symbol, {}).values(),
            key=lambda item: item["trade_date"],
        )[-required_history_bars:]
        target_bar = histories.get(symbol, {}).get(target_trade_date)
        basic = basics.get(symbol)
        result.append(
            {
                **snapshot,
                "close_price": target_bar.get("close") if target_bar else None,
                "pct_chg": target_bar.get("change_pct") if target_bar else None,
                "daily_basic_present": basic is not None,
                "pe_ttm": basic.get("pe_ttm") if basic else None,
                "pb": basic.get("pb") if basic else None,
                "total_mv": basic.get("total_mv") if basic else None,
                "circ_mv": basic.get("circ_mv") if basic else None,
                "history": history,
                "expected_history_dates": expected_dates,
                "factor_values": {
                    factor_id: values[symbol]
                    for factor_id, values in supplied.items()
                    if symbol in values
                },
            }
        )
    return result


def update_due_evaluations(*, limit: int = 500) -> dict[str, int]:
    contexts = repository.pending_evaluation_contexts(limit=limit)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for context in contexts:
        grouped[str(context["target_trade_date"])].append(context)
    counts = {
        "updated": 0,
        "completed": 0,
        "insufficient_data": 0,
        "pending": 0,
        "failed": 0,
    }
    for target_trade_date, target_contexts in grouped.items():
        try:
            future_dates = load_local_open_trade_dates(
                start_exclusive=target_trade_date,
                limit=max(int(context["horizon"]) for context in target_contexts),
            )
        except Exception as exc:
            counts["failed"] += len(target_contexts)
            for context in target_contexts:
                repository.update_evaluation(int(context["evaluation_id"]), {"error": str(exc)[:1000]})
            logger.warning("自动选股评估日历不可用 target_trade_date=%s error=%s", target_trade_date, exc)
            continue
        due_contexts = [
            context
            for context in target_contexts
            if len(future_dates) >= int(context["horizon"])
        ]
        counts["pending"] += len(target_contexts) - len(due_contexts)
        due_ids = {context["evaluation_id"] for context in due_contexts}
        for context in target_contexts:
            if context["evaluation_id"] not in due_ids:
                repository.update_evaluation(int(context["evaluation_id"]), {})
        if not due_contexts:
            continue

        symbols = sorted(
            {
                str(symbol)
                for context in due_contexts
                for symbol in [
                    *context["selected_symbols"],
                    *context["benchmark_symbols"],
                ]
            }
        )
        evaluation_dates = future_dates[
            : max(int(context["horizon"]) for context in due_contexts)
        ]
        bars_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        normalized_date = func.replace(DailyBar.trade_date, "-", "")
        with SessionLocal() as db:
            for symbol_batch in _chunks(symbols, 500):
                bar_rows = db.execute(
                    select(
                        DailyBar.id,
                        DailyBar.symbol,
                        normalized_date.label("trade_date"),
                        DailyBar.open,
                        DailyBar.close,
                    )
                    .where(
                        DailyBar.source == "tushare",
                        DailyBar.symbol.in_(symbol_batch),
                        DailyBar.trade_date.in_(_date_variants(evaluation_dates)),
                    )
                    .order_by(DailyBar.id)
                ).all()
                for _, symbol, trade_date, open_price, close_price in bar_rows:
                    key = (str(symbol), str(trade_date))
                    bars_by_key[key] = {
                        "symbol": key[0],
                        "trade_date": key[1],
                        "open": open_price,
                        "close": close_price,
                    }
            adj_factors = load_local_adj_factors(db, symbols, evaluation_dates)
        for key, bar in bars_by_key.items():
            bar["adj_factor"] = adj_factors.get(key)
        bar_payload = list(bars_by_key.values())

        for context in due_contexts:
            try:
                config = validate_config(dict(context.get("config") or {}))
                costs = config["costs"]
                quality = config["quality"]
                result = _evaluate_context(
                    context,
                    bars=bar_payload,
                    trading_dates=future_dates,
                    min_coverage=float(quality["min_evaluation_coverage"]),
                    commission_rate=float(costs["commission_rate"]),
                    slippage_rate=float(costs["slippage_rate"]),
                )
                repository.update_evaluation_result(
                    int(context["evaluation_id"]),
                    result,
                )
                status = str(result["status"])
                counts["updated"] += 1
                counts[status] += 1
            except Exception as exc:
                counts["failed"] += 1
                logger.warning(
                    "自动选股前瞻评估失败 evaluation_id=%s run_id=%s error=%s",
                    context.get("evaluation_id"),
                    context.get("run_id"),
                    exc,
                    exc_info=True,
                )
                repository.update_evaluation(
                    int(context["evaluation_id"]),
                    {
                        "status": "insufficient_data",
                        "error": f"评估失败，等待后续重试：{str(exc)[:1000]}",
                        "evaluated_at": datetime.now(),
                    },
                )
                counts["updated"] += 1
                counts["insufficient_data"] += 1
    return counts


def _evaluate_context(
    context: dict[str, Any],
    *,
    bars: list[dict[str, Any]],
    trading_dates: list[str],
    min_coverage: float,
    commission_rate: float,
    slippage_rate: float,
) -> dict[str, Any]:
    from backend.app.quant.automatic_selection_evaluation import evaluate_forward_window

    return evaluate_forward_window(
        target_trade_date=str(context["target_trade_date"]),
        horizon=int(context["horizon"]),
        selected_symbols=context["selected_symbols"],
        benchmark_symbols=context["benchmark_symbols"],
        bars=bars,
        trading_dates=trading_dates,
        min_coverage=min_coverage,
        commission_rate=commission_rate,
        slippage_rate=slippage_rate,
        turnover=context.get("turnover"),
    )


def _sync_required_factors(
    factor_ids: Iterable[str],
    target_trade_date: str,
) -> dict[str, Any]:
    names = sorted(
        {
            name
            for factor_id in factor_ids
            if (name := parse_tushare_factor_id(factor_id))
        }
    )
    if not names:
        return {
            "requested": [],
            "stored_rows": 0,
            "errors": [],
            "batches": [],
        }
    try:
        ensure_catalog()
        known_names = known_factor_names()
    except Exception as exc:
        return {
            "requested": names,
            "stored_rows": 0,
            "errors": [f"TuShare 因子目录不可用：{exc}"],
            "batches": [],
        }

    unknown = sorted(set(names) - known_names)
    sync_names = sorted(set(names) & known_names)
    errors = [
        f"TuShare 因子目录中不存在：{name}"
        for name in unknown
    ]
    summaries: list[dict[str, Any]] = []
    for start in range(0, len(sync_names), MAX_SYNC_FACTORS):
        batch = sync_names[start : start + MAX_SYNC_FACTORS]
        try:
            summaries.append(
                sync_factor_values(
                    batch,
                    start_date=target_trade_date,
                    end_date=target_trade_date,
                )
            )
        except Exception as exc:
            errors.append(
                f"TuShare 因子批次 {','.join(batch)} 同步失败：{exc}"
            )
    errors.extend(
        str(error)
        for item in summaries
        for error in item.get("errors") or []
    )
    return {
        "requested": names,
        "stored_rows": sum(
            int(item.get("stored_rows") or 0)
            for item in summaries
        ),
        "errors": errors,
        "batches": summaries,
    }


def _load_target_factor_values(
    factor_ids: Iterable[str], target_trade_date: str,
) -> dict[str, dict[str, float]]:
    values: dict[str, dict[str, float]] = {}
    for factor_id in sorted(set(factor_ids)):
        factor_name = parse_tushare_factor_id(factor_id)
        if not factor_name:
            continue
        panel = load_factor_panel(
            factor_name,
            start_date=target_trade_date,
            end_date=target_trade_date,
        )
        if panel is None or panel.empty:
            values[factor_id] = {}
            continue
        target_rows = [index for index in panel.index if index.strftime("%Y%m%d") == target_trade_date]
        if len(target_rows) != 1:
            values[factor_id] = {}
            continue
        row = panel.loc[target_rows[0]]
        factor_values: dict[str, float] = {}
        for symbol, value in row.items():
            number = _finite_float(value)
            if number is not None:
                factor_values[str(symbol)] = number
        values[factor_id] = factor_values
    return values


@contextmanager
def _database_named_lock(
    lock_name: str,
    busy_message: str,
) -> Iterator[None]:
    db = SessionLocal()
    acquired = True
    try:
        if db.get_bind().dialect.name == "mysql":
            acquired = bool(
                db.execute(
                    text("SELECT GET_LOCK(:lock_name, 0)"),
                    {"lock_name": lock_name},
                ).scalar()
            )
        if not acquired:
            raise RuntimeError(busy_message)
        yield
    finally:
        if acquired and db.get_bind().dialect.name == "mysql":
            try:
                db.execute(
                    text("SELECT RELEASE_LOCK(:lock_name)"),
                    {"lock_name": lock_name},
                )
            except Exception:
                logger.warning(
                    "释放自动选股数据库锁失败 lock_name=%s",
                    lock_name,
                    exc_info=True,
                )
        db.close()


@contextmanager
def _selection_lock() -> Iterator[None]:
    with _database_named_lock(
        _LOCK_NAME,
        "自动选股任务正在其他实例运行，请稍后重试",
    ):
        yield


@contextmanager
def _ingestion_lock() -> Iterator[None]:
    with _database_named_lock(
        _LOCK_NAME,
        "自动选股数据入库或排名正在其他实例运行，请稍后重试",
    ):
        yield


def _is_supported_a_share(symbol: Any) -> bool:
    value = str(symbol or "").upper()
    if value.endswith(".SH"):
        return value[:2] in {"60", "68"}
    if value.endswith(".SZ"):
        return value[:2] in {"00", "30"}
    if value.endswith(".BJ"):
        return value[:1] in {"4", "8", "9"}
    return False


def run_strategy_from_rows(
    strategy: dict[str, Any],
    rows: Iterable[dict[str, Any]],
    *,
    target_trade_date: str,
    trigger_type: str,
    job_run_id: str | None = None,
    sync_summary: dict[str, Any] | None = None,
    generate_explanation: bool = True,
    defer_explanation: bool = False,
) -> dict[str, Any]:
    config = validate_config(dict(strategy.get("config") or {}))
    if config_hash(config) != strategy.get("config_hash"):
        raise ValueError("策略配置哈希不一致或缺少当前必需门禁，请创建新版本，不得修改旧版本")
    candidates = build_candidates(rows, config, target_trade_date=target_trade_date)
    current_universe_count = len(candidates)
    gate = quality_gate(candidates, config)
    previous_run = repository.latest_run_model(
        int(strategy["id"]),
        before_trade_date=target_trade_date,
        completed_only=True,
    )
    previous_items = (
        repository.selected_candidate_snapshots(
            previous_run.run_id
        )
        if previous_run is not None
        else []
    )
    previous_symbols = sorted(str(item["symbol"]) for item in previous_items)
    previous_summary = json.loads(previous_run.sync_summary_json or "{}") if previous_run is not None else {}
    expected_dates = next(
        (list(item.get("input_evidence", {}).get("expected_history_dates") or []) for item in candidates),
        [],
    )
    rebalance = rebalance_state(
        config,
        previous_anchor=(previous_summary.get("rebalance") or {}).get("anchor_trade_date") if previous_symbols else None,
        expected_dates=expected_dates,
        target_trade_date=target_trade_date,
    )
    fingerprint = fingerprint_payload(
        candidates,
        config,
        target_trade_date,
        previous_selection={
            "run_id": previous_run.run_id if previous_run is not None else None,
            "data_fingerprint": (
                previous_run.data_fingerprint
                if previous_run is not None
                else None
            ),
            "selected_symbols": previous_symbols,
            **({"rebalance": rebalance} if rebalance is not None else {}),
        },
    )
    source_fingerprint = fingerprint
    latest_same_date = repository.latest_run_for_target_model(int(strategy["id"]), target_trade_date)
    if latest_same_date is not None:
        latest_summary = json.loads(latest_same_date.sync_summary_json or "{}")
        if (
            latest_same_date.status in {"completed", "blocked"}
            and latest_summary.get("source_fingerprint") == source_fingerprint
        ):
            fingerprint = latest_same_date.data_fingerprint
    existing = repository.find_run_by_inputs(
        int(strategy["id"]),
        target_trade_date,
        str(strategy["config_hash"]),
        fingerprint,
    )
    if existing is not None and latest_same_date is not None and existing["run_id"] != latest_same_date.run_id:
        fingerprint = data_fingerprint(source_fingerprint, {"supersedes_run_id": latest_same_date.run_id})
        existing = repository.find_run_by_inputs(
            int(strategy["id"]), target_trade_date, str(strategy["config_hash"]), fingerprint,
        )
    if existing is not None:
        if (
            generate_explanation
            and not defer_explanation
            and existing.get("status") == "completed"
            and existing.get("llm_status") == "pending"
        ):
            _generate_persisted_explanation(str(existing["run_id"]))
            existing = _safe_run_detail(str(existing["run_id"])) or existing
        return {**existing, "created": False}

    now = datetime.now()
    selected_turnover: float | None = None
    if gate["passed"]:
        score_candidates(candidates, config, previous_selected=previous_symbols, rebalance=rebalance)
        changes = apply_membership_changes(candidates, previous_symbols)
        _append_missing_removed_candidates(candidates, previous_items)
        current_symbols = [str(item["symbol"]) for item in candidates if item.get("selected")]
        selected_turnover = calculate_turnover(current_symbols, previous_symbols)
        status = "completed"
        stage = "ranked"
        llm_status = "pending" if generate_explanation else "skipped"
        selected_count = len(current_symbols)
    else:
        changes = {"added": 0, "retained": 0, "removed": 0}
        status = "blocked"
        stage = "quality_gate"
        llm_status = "skipped"
        selected_count = 0

    payload = {
        "strategy_id": int(strategy["id"]),
        "strategy_key": str(strategy["strategy_key"]),
        "strategy_version": int(strategy["version"]),
        "strategy_name": str(strategy["name"]),
        "target_trade_date": target_trade_date,
        "trigger_type": str(trigger_type or "manual")[:24],
        "job_run_id": job_run_id,
        "status": status,
        "stage": stage,
        "config_json": canonical_json(config),
        "config_hash": str(strategy["config_hash"]),
        "data_fingerprint": fingerprint,
        "supersedes_run_id": latest_same_date.run_id if latest_same_date is not None else None,
        "sync_summary_json": canonical_json({
            **(sync_summary or {}),
            "scoring_implementation_version": SCORING_IMPLEMENTATION_VERSION,
            "source_fingerprint": source_fingerprint,
            "membership_changes": changes,
            # 下一次运行只从“已完成”的运行里读换仓锚点，被门禁拦下的这条不会影响它
            **({"rebalance": rebalance} if rebalance is not None else {}),
        }),
        "quality_gate_json": canonical_json(gate),
        "exclusion_summary_json": canonical_json(exclusion_summary(candidates)),
        "universe_count": current_universe_count,
        "eligible_count": sum(bool(item.get("eligible")) for item in candidates),
        "selected_count": selected_count,
        "selected_turnover": selected_turnover,
        "llm_status": llm_status,
        "started_at": now,
        "finished_at": datetime.now(),
    }
    persisted, created = repository.persist_run(
        run_payload=payload,
        candidates=candidates,
        create_evaluations=status == "completed",
    )
    if (
        created
        and status == "completed"
        and generate_explanation
        and not defer_explanation
    ):
        _generate_persisted_explanation(str(persisted["run_id"]))
        persisted = _safe_run_detail(str(persisted["run_id"])) or persisted
    return {**persisted, "created": created}


def _persist_failed_strategy_run(
    strategy: dict[str, Any],
    *,
    target_trade_date: str,
    trigger_type: str,
    job_run_id: str | None,
    sync_summary: dict[str, Any] | None,
    error: Exception,
) -> dict[str, Any]:
    failure_message = str(error)[:4000]
    fingerprint = data_fingerprint(
        SCORING_IMPLEMENTATION_VERSION,
        "failed",
        strategy.get("id"),
        strategy.get("config_hash"),
        target_trade_date,
        type(error).__name__,
        failure_message,
    )
    latest_same_date = repository.latest_run_for_target_model(
        int(strategy["id"]), target_trade_date
    )
    now = datetime.now()
    persisted, created = repository.persist_run(
        run_payload={
            "strategy_id": int(strategy["id"]),
            "strategy_key": str(strategy["strategy_key"]),
            "strategy_version": int(strategy["version"]),
            "strategy_name": str(strategy["name"]),
            "target_trade_date": target_trade_date,
            "trigger_type": str(trigger_type or "manual")[:24],
            "job_run_id": job_run_id,
            "status": "failed",
            "stage": "failed",
            "config_json": canonical_json(dict(strategy.get("config") or {})),
            "config_hash": str(strategy["config_hash"]),
            "data_fingerprint": fingerprint,
            "supersedes_run_id": latest_same_date.run_id if latest_same_date is not None else None,
            "sync_summary_json": canonical_json({
                **(sync_summary or {}),
                "scoring_implementation_version": SCORING_IMPLEMENTATION_VERSION,
            }),
            "quality_gate_json": canonical_json(
                {
                    "passed": False,
                    "metrics": {},
                    "thresholds": dict(
                        (strategy.get("config") or {}).get("quality") or {}
                    ),
                    "failures": [{"code": "strategy_error", "message": failure_message}],
                }
            ),
            "exclusion_summary_json": "{}",
            "universe_count": 0,
            "eligible_count": 0,
            "selected_count": 0,
            "llm_status": "skipped",
            "error": failure_message,
            "started_at": now,
            "finished_at": now,
        },
        candidates=[],
        create_evaluations=False,
    )
    return {**persisted, "created": created}


def run_strategies_from_rows(
    strategies: Iterable[dict[str, Any]],
    rows: Iterable[dict[str, Any]],
    *,
    target_trade_date: str,
    trigger_type: str,
    job_run_id: str | None = None,
    sync_summary: dict[str, Any] | None = None,
    generate_explanation: bool = True,
    defer_explanation: bool = False,
) -> dict[str, Any]:
    source_rows = list(rows)
    results: list[dict[str, Any]] = []
    for strategy in strategies:
        try:
            result = run_strategy_from_rows(
                strategy,
                source_rows,
                target_trade_date=target_trade_date,
                trigger_type=trigger_type,
                job_run_id=job_run_id,
                sync_summary=sync_summary,
                generate_explanation=generate_explanation,
                defer_explanation=defer_explanation,
            )
        except Exception as exc:
            logger.exception(
                "自动选股策略运行失败 strategy_id=%s target_trade_date=%s",
                strategy.get("id"),
                target_trade_date,
            )
            try:
                result = _persist_failed_strategy_run(
                    strategy,
                    target_trade_date=target_trade_date,
                    trigger_type=trigger_type,
                    job_run_id=job_run_id,
                    sync_summary=sync_summary,
                    error=exc,
                )
            except Exception:
                logger.exception(
                    "自动选股策略失败证据落库失败 strategy_id=%s target_trade_date=%s",
                    strategy.get("id"),
                    target_trade_date,
                )
                result = {
                    "strategy_id": strategy.get("id"),
                    "strategy_key": strategy.get("strategy_key"),
                    "strategy_version": strategy.get("version"),
                    "status": "failed",
                    "error": str(exc),
                    "created": False,
                }
        results.append(result)
    completed = sum(item.get("status") == "completed" for item in results)
    blocked = sum(item.get("status") == "blocked" for item in results)
    failed = sum(item.get("status") == "failed" for item in results)
    return {
        "target_trade_date": target_trade_date,
        "strategy_count": len(results),
        "completed_count": completed,
        "blocked_count": blocked,
        "failed_count": failed,
        "selected_count": sum(int(item.get("selected_count") or 0) for item in results),
        "runs": results,
    }


def _append_missing_removed_candidates(
    candidates: list[dict[str, Any]], previous_items: Iterable[dict[str, Any]],
) -> None:
    current_symbols = {str(item.get("symbol") or "") for item in candidates}
    for previous in previous_items:
        symbol = str(previous.get("symbol") or "")
        if not symbol or symbol in current_symbols:
            continue
        candidates.append(
            {
                "symbol": symbol,
                "name": previous.get("name") or symbol,
                "market": previous.get("market"),
                "list_status": previous.get("list_status"),
                "list_date": previous.get("list_date"),
                "industry": previous.get("industry"),
                "market_cap_bucket": previous.get("market_cap_bucket"),
                "eligible": False,
                "exclusion_codes": ["not_in_current_catalog"],
                "constraint_codes": [],
                "close_price": None,
                "pct_chg": None,
                "pe_ttm": None,
                "pb": None,
                "total_mv": None,
                "circ_mv": None,
                "avg_amount_20": None,
                "momentum_20": None,
                "volatility_20": None,
                "factor_values": {},
                "z_scores": {},
                "composite_score": None,
                "rank": None,
                "percentile": None,
                "selected": False,
                "research_weight": None,
                "change_state": "removed",
            }
        )


def _safe_run_detail(run_id: str) -> dict[str, Any] | None:
    try:
        return repository.run_detail(run_id, candidate_scope="top")
    except Exception:
        logger.warning("读取自动选股运行详情失败 run_id=%s", run_id, exc_info=True)
        return None


def _generate_persisted_explanation(run_id: str) -> None:
    try:
        detail = repository.run_detail(run_id, candidate_scope="top")
        if detail is None:
            raise RuntimeError("已落库自动选股证据不存在")
        _generate_explanation(
            detail,
            detail.get("candidates") or [],
            detail.get("quality_gate") or {},
        )
    except Exception as exc:
        logger.warning("自动选股解释阶段失败 run_id=%s error=%s", run_id, exc)
        try:
            repository.update_run_explanation(
                run_id,
                status="failed",
                error=str(exc)[:4000],
            )
        except Exception:
            logger.exception("自动选股解释失败状态落库失败 run_id=%s", run_id)


def _generate_explanation(
    run: dict[str, Any], candidates: Iterable[dict[str, Any]], gate: dict[str, Any],
) -> None:
    run_id = str(run["run_id"])
    try:
        llm_config = active_llm_config()
        params = reasoning_params(llm_config)
        candidates = list(candidates)
        try:
            evidence = explanation_evidence(run, candidates)
        except Exception:
            logger.warning("自动选股白话证据生成失败，退回原始证据 run_id=%s", run_id, exc_info=True)
            evidence = {
                "target_trade_date": run.get("target_trade_date"),
                "quality_gate": gate,
                "selected": [
                    {
                        "rank": item.get("rank"),
                        "symbol": item.get("symbol"),
                        "name": item.get("name"),
                        "industry": item.get("industry"),
                        "percentile": item.get("percentile"),
                        "change_state": item.get("change_state"),
                    }
                    for item in candidates
                    if item.get("selected")
                ],
                "limitations": RESEARCH_LIMITATIONS,
            }
        messages = [
            {
                "role": "system",
                "content": (
                    "你是自动选股点评员，读者是没有量化背景的普通投资者。排名和分数已经由确定性规则完成并落库，"
                    "你只能解释给定证据和风险，不得新增事实、改分、重排、给出买卖指令或目标价，"
                    "不得声称收益可预测。用中文白话，不要出现 z 值、因子、哈希等术语。"
                    "维度分数表示这只股票超过了百分之多少的合格股票。输出 Markdown，格式固定：\n"
                    "1. 第一行以“**今日要点**：”开头，用一句话（不超过 60 字）概括这批股票的共同特点；\n"
                    "2. 然后每只股票一行：“- 名称（代码）：入选原因，最需要注意的一点”，不超过 40 字；\n"
                    "3. 最后一行写：以上为规则选股的研究观察，不构成投资建议。"
                ),
            },
            {
                "role": "user",
                "content": f"请点评以下已落库的选股结果：\n{canonical_json(evidence)}",
            },
        ]
        response = chat_completion(
            messages,
            llm_config,
            enable_thinking=params.get("enable_thinking"),
            reasoning_effort=params.get("reasoning_effort"),
            timeout=180.0,
            log_module="automatic_selection",
        )
        content = str(((response.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        if not content:
            raise RuntimeError("模型未返回自动选股解释")
        repository.update_run_explanation(
            run_id,
            status="completed",
            model=str(llm_config.get("display_name") or llm_config.get("model") or "") or None,
            explanation=content,
        )
    except Exception as exc:
        logger.warning("自动选股解释生成失败 run_id=%s error=%s", run_id, exc)
        repository.update_run_explanation(
            run_id,
            status="failed",
            error=str(exc)[:4000],
        )
