"""离线历史回测与校准引擎（P3）：检验 L1 大盘仓位刹车 + 分层一票否决能否在真实执行约束下压降 model-rank-30 的回撤并提升夏普。

用法：
    python -m backend.app.quant.research_layered_selection dev --out ~/hermes-stock-research
    python -m backend.app.quant.research_layered_selection final --out ~/hermes-stock-research

严谨纪律：
- dev 仅使用 2018-12-31 及以前数据校准阈值；final 在 2019 年及以后样本外验证。
- 严格次日开盘成交，处理停牌与一字涨跌停不可成交、双边佣金印花税滑点（BASE_COSTS / STRESS_COSTS 2倍压力测试）。
- L1 仓位上限严格只用决策日收盘及以前的截面宽度与成交分位，零未来函数。
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backend.app.quant import research_models as rm
from backend.app.quant import research_selection as rs
from backend.app.quant.research_features import DEV_END, DEV_START, compute_all, forward_return
from backend.app.quant.research_panel import Panel


def panel_regime_caps(
    panel: Panel,
    *,
    attack_breadth: float = 0.50,
    defense_breadth: float = 0.25,
    caps: tuple[float, float, float] = (0.90, 0.60, 0.30),
) -> np.ndarray:
    """向量化计算历史每日 L1 建议总仓位上限（0.90 / 0.60 / 0.30），严格只用当日及过去数据。"""
    close_df = pd.DataFrame(np.asarray(panel.adj_close_filled, dtype=np.float64))
    ma20 = close_df.rolling(20, min_periods=15).mean().to_numpy()
    ma60 = close_df.rolling(60, min_periods=45).mean().to_numpy()
    close_arr = close_df.to_numpy()
    univ = panel.universe

    valid20 = univ & np.isfinite(ma20) & np.isfinite(close_arr)
    valid60 = univ & np.isfinite(ma60) & np.isfinite(close_arr)
    with np.errstate(invalid="ignore", divide="ignore"):
        b20 = np.where(valid20.sum(axis=1) >= 10, (valid20 & (close_arr >= ma20)).sum(axis=1) / np.maximum(valid20.sum(axis=1), 1), 0.50)
        b60 = np.where(valid60.sum(axis=1) >= 10, (valid60 & (close_arr >= ma60)).sum(axis=1) / np.maximum(valid60.sum(axis=1), 1), b20)

    amt_raw = np.asarray(panel.field("daily", "amount"), dtype=np.float64)
    total_amt = pd.Series(np.nansum(np.where(univ, amt_raw, np.nan), axis=1))
    vol_pct = total_amt.rolling(120, min_periods=10).rank(pct=True).fillna(0.50).to_numpy()

    breadth_score = (0.60 * b20 + 0.40 * b60) * 100.0
    regime_score = 0.65 * breadth_score + 0.35 * (vol_pct * 100.0)

    out = np.full(len(panel.dates), caps[1], dtype=np.float64)
    out[(regime_score >= 65.0) & (b20 >= attack_breadth)] = caps[0]
    out[(regime_score < 40.0) | (b20 < defense_breadth)] = caps[2]
    return out


def panel_downtrend_knife_veto(panel: Panel) -> np.ndarray:
    """向量化计算波段破位接飞刀否决掩码（True 表示触发一票否决）。"""
    close_df = pd.DataFrame(np.asarray(panel.adj_close_filled, dtype=np.float64))
    ma20 = close_df.rolling(20, min_periods=20).mean().to_numpy()
    ma60 = close_df.rolling(60, min_periods=60).mean().to_numpy()
    ret20 = close_df.pct_change(20).to_numpy()
    close_arr = close_df.to_numpy()

    with np.errstate(invalid="ignore"):
        return (
            np.isfinite(ma60)
            & np.isfinite(ma20)
            & np.isfinite(ret20)
            & (close_arr < ma60 * 0.92)
            & (ma20 < ma60)
            & (ret20 < -0.12)
        )


def apply_veto_to_scores(scores: np.ndarray, veto_mask: np.ndarray) -> np.ndarray:
    """将一票否决标的的得分置为 NaN，确保换仓时已持有者优先卖出、未持有者绝不买入。"""
    cleaned = np.array(scores, dtype=np.float32, copy=True)
    cleaned[veto_mask] = np.nan
    return cleaned


def _rebalance_with_cap(
    panel: Panel,
    day: int,
    score: np.ndarray,
    target_cap: float,
    shares: dict[int, float],
    cash: float,
    top_n: int,
    band: float,
    costs: rs.Costs,
) -> tuple[float, float]:
    """次日开盘换仓并执行 L1 总仓位上限控制（处理停牌与一字涨跌停不可成交）。"""
    open_price = panel.adjusted("open")[day]
    close_fallback = panel.adj_close_filled[day]
    traded, locked = panel.traded[day], panel.one_price[day]
    raw_close, pre_close = panel.field("daily", "close")[day], panel.field("daily", "pre_close")[day]

    order = np.argsort(-np.where(np.isnan(score), -np.inf, score), kind="stable")
    order = order[: int((~np.isnan(score)).sum())]
    rank_of = {int(stock): pos for pos, stock in enumerate(order.tolist())}
    sold = 0.0

    # 1. 先卖出跌出缓冲带或被否决的持仓
    for stock in list(shares):
        if rank_of.get(stock, math.inf) < band * top_n:
            continue
        locked_down = bool(locked[stock]) and float(raw_close[stock]) < float(pre_close[stock])
        if not traded[stock] or locked_down or not np.isfinite(open_price[stock]):
            continue
        proceeds = shares.pop(stock) * float(open_price[stock])
        sold += proceeds
        cash += proceeds * (1.0 - costs.sell)

    # 2. 若当前留存持仓市值仍超出 L1 总仓位上限，按比例等权削减可成交持仓（大盘转防守时主动降仓）
    def _val_of(stk: int) -> float:
        px = float(open_price[stk]) if np.isfinite(open_price[stk]) and open_price[stk] > 0 else float(close_fallback[stk])
        return shares[stk] * px

    current_holdings_val = sum(_val_of(s) for s in shares)
    total_equity = cash + current_holdings_val
    max_investable = total_equity * max(0.0, min(1.0, target_cap))

    if current_holdings_val > max_investable * 1.02 and current_holdings_val > 0:
        trim_ratio = 1.0 - (max_investable / current_holdings_val)
        for stock in list(shares):
            locked_down = bool(locked[stock]) and float(raw_close[stock]) < float(pre_close[stock])
            if not traded[stock] or locked_down or not np.isfinite(open_price[stock]):
                continue
            sell_qty = shares[stock] * trim_ratio
            proceeds = sell_qty * float(open_price[stock])
            shares[stock] -= sell_qty
            if shares[stock] <= 1e-9:
                shares.pop(stock, None)
            sold += proceeds
            cash += proceeds * (1.0 - costs.sell)

    # 3. 用不超过 L1 上限的可用额度补齐剩余席位
    current_holdings_val = sum(_val_of(s) for s in shares)
    total_equity = cash + current_holdings_val
    avail_for_new = min(cash, max(0.0, total_equity * target_cap - current_holdings_val))
    slots = top_n - len(shares)
    if slots <= 0 or avail_for_new <= 1e-9:
        return cash, sold

    budget = avail_for_new / slots
    for stock in order.tolist():
        if slots <= 0:
            break
        if stock in shares or not traded[stock] or locked[stock] or not np.isfinite(open_price[stock]):
            continue
        shares[stock] = budget / (float(open_price[stock]) * (1.0 + costs.buy))
        cash -= budget
        slots -= 1
    return cash, sold


def simulate_layered(
    panel: Panel,
    scores: np.ndarray,
    position_caps: np.ndarray,
    *,
    top_n: int = 30,
    start: str,
    end: str,
    offset: int = 0,
    hold_days: int = rs.HOLD_DAYS,
    band: float = rs.HOLD_BAND,
    costs: rs.Costs = rs.BASE_COSTS,
) -> dict[str, Any]:
    """收盘决策（含当日 L1 仓位上限与否决后排名），次日开盘成交。"""
    dates = np.array(panel.dates)
    rows = np.flatnonzero((dates >= start) & (dates <= end))
    close = panel.adj_close_filled
    decide = set(rows[offset::hold_days].tolist())

    shares: dict[int, float] = {}
    cash, sold_total = 1.0, 0.0
    pending: tuple[np.ndarray, float] | None = None
    nav = np.empty(len(rows))
    realized_pos = np.zeros(len(rows))

    for k, day in enumerate(rows.tolist()):
        if pending is not None:
            pend_score, pend_cap = pending
            cash, sold = _rebalance_with_cap(panel, day, pend_score, pend_cap, shares, cash, top_n, band, costs)
            sold_total += sold
            pending = None
        hold_val = sum(amt * float(close[day, stk]) for stk, amt in shares.items())
        nav[k] = cash + hold_val
        realized_pos[k] = (hold_val / nav[k]) if nav[k] > 0 else 0.0
        if day in decide and k + 1 < len(rows):
            pending = (np.asarray(scores[day], dtype=np.float64), float(position_caps[day]))

    return {
        "dates": dates[rows],
        "nav": nav,
        "sold": sold_total,
        "avg_position_ratio": float(realized_pos.mean()) if len(realized_pos) else 0.0,
    }


def compare_baseline_vs_layered(
    panel: Panel,
    raw_model_scores: np.ndarray,
    *,
    top_n: int = 30,
    start: str,
    end: str,
) -> dict[str, Any]:
    """在完全相同的行情面板与交易成本下，对比纯量价 model-rank-30 与 L1+Veto 分层策略。"""
    eligible = rs.pool(panel, rs.MIN_AMOUNT)
    bench = rs.benchmark_nav(panel, eligible, start, end)

    masked_base = np.where(eligible, raw_model_scores, np.nan).astype(np.float32)
    caps = panel_regime_caps(panel)
    knife_veto = panel_downtrend_knife_veto(panel)
    layered_scores = apply_veto_to_scores(masked_base, knife_veto)

    base_summary = rs.evaluate(panel, masked_base, bench, top_n=top_n, start=start, end=end)

    layered_runs = []
    layered_stress = []
    avg_positions = []
    for offset in range(rs.HOLD_DAYS):
        r_base = simulate_layered(panel, layered_scores, caps, top_n=top_n, start=start, end=end, offset=offset, costs=rs.BASE_COSTS)
        r_stress = simulate_layered(panel, layered_scores, caps, top_n=top_n, start=start, end=end, offset=offset, costs=rs.STRESS_COSTS)
        layered_runs.append(rs.metrics(r_base["dates"], r_base["nav"], bench, r_base["sold"]))
        layered_stress.append(rs.metrics(r_stress["dates"], r_stress["nav"], bench, r_stress["sold"]))
        avg_positions.append(r_base["avg_position_ratio"])

    layered_summary = rs.summarize(layered_runs, layered_stress)
    layered_summary["avg_position_ratio"] = float(np.median(avg_positions))
    layered_summary["drawdown_improvement"] = float(layered_summary["drawdown"] - base_summary["drawdown"])
    layered_summary["sharpe_delta"] = float(layered_summary["sharpe"] - base_summary["sharpe"])

    return {
        "window": {"start": start, "end": end, "top_n": top_n},
        "baseline_model_rank": base_summary,
        "layered_l1_veto": layered_summary,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="L1 仓位刹车 + 分层一票否决离线验证")
    parser.add_argument("stage", choices=("dev", "final"))
    parser.add_argument("--out", required=True, help="研究数据与输出目录")
    parser.add_argument("--top-n", type=int, default=30)
    args = parser.parse_args(argv)

    out_dir = Path(args.out).expanduser().resolve()
    from backend.app.quant.research_panel import load_panel

    panel = load_panel(out_dir)
    features = compute_all(panel, names=rm.RANK_MODEL_FEATURES)
    label = forward_return(panel, rm.HORIZON)

    if args.stage == "dev":
        raw_scores = rs.model_scores(panel, features, label, first_year=rs.FIRST_DEV_YEAR, end=DEV_END)
        start, end = f"{rs.FIRST_DEV_YEAR}0101", DEV_END
    else:
        raw_scores = rs.model_scores(panel, features, label, first_year=rs.FIRST_FINAL_YEAR, end=panel.dates[-1])
        start, end = f"{rs.FIRST_FINAL_YEAR}0101", panel.dates[-1]

    report = compare_baseline_vs_layered(panel, raw_scores, top_n=args.top_n, start=start, end=end)
    target_path = out_dir / f"layered_selection_{args.stage}.json"
    target_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
