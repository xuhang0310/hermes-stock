"""量化选股（L1–L7）分层打分。现状、哪些验证过、和操作计划怎么统一，见 docs/quant-selection.md。

- L1 大盘环境（market_regime）只给总仓位上限和风格，不进个股得分。仓位上限还没有回测，只作参考。
- L2–L6 按行业、基本面、预期差、时机、事件打分，三套权重（波段/长线/事件）合成总分，分高分、中等、低分或排除。
  权重、门槛、破位和阴跌否决都是人定的，没有回测；时机层里的模型排名按市值分组重排过，已经不是验证过的那个排名。
- 财报按真实披露日（ann_date <= trade_date）取，全市场重大公告（立案调查、退市风险、财务造假）直接排除。
- 挂买价、止损下限和股数和操作计划用同一套代码（compute_execution_levels、compute_l7_position_sizing）。
- 每天收盘后算一遍存 stock_selection_daily_snapshot；波段总分同时是自动选股挑战者 layered-swing-30 的唯一特征。
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Sequence

import pandas as pd
from sqlalchemy import case, delete, func, insert, not_, or_, select
from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal
from backend.app.core.models import (
    DailyBar,
    Instrument,
    Position,
    PositionAccount,
    StockDailyBasic,
    StockModelSignal,
    StockSelectionDailySnapshot,
)
from backend.app.market.event_signal_sync import VETO_POLARITY, load_pit_event_signals
from backend.app.market.financial_pit_sync import load_pit_financials
from backend.app.quant.market_regime import (
    compute_and_store_market_regime,
    get_latest_market_regime,
)
from backend.app.plans.rules import limit_ratio as plan_limit_ratio
from backend.app.quant.order_engine import (
    STOP_ATR,
    load_table as load_order_table,
    model_buy_price,
    plan_orders,
)
from backend.app.quant.tech_signal import Bar, _atr, adjusted_bars
from backend.app.shadow import rules as shadow_rules

logger = logging.getLogger(__name__)

PLAN_SWING = "swing"
PLAN_LONG = "long"
PLAN_EVENT = "event"
VALID_PLANS = (PLAN_SWING, PLAN_LONG, PLAN_EVENT)

PLAN_LABELS = {
    PLAN_SWING: "波段交易（5–15日）",
    PLAN_LONG: "中长线配置（30–120日）",
    PLAN_EVENT: "事件与预期差（3–10日）",
}

# 打分没有回测，结论只说分数高低，不说买不买；代码值沿用 action/watch/pass，库里和接口不变
VERDICT_ACTION = "action"  # ≥70 且没被排除且覆盖率达标
VERDICT_WATCH = "watch"    # 55–69，或高分但数据不全
VERDICT_PASS = "pass"      # <55 或被排除

VERDICT_LABELS = {
    VERDICT_ACTION: "高分",
    VERDICT_WATCH: "中等",
    VERDICT_PASS: "低分或排除",
}
# 改打分逻辑（权重、门槛、否决、特征）就改这个版本号；自动选股的分层挑战者会记下每天用的是哪一版
SCORE_VERSION = "layered-2026-11-18"

# L2–L6 层权重配置（总和严格 = 1.0；L1 绝不进权重）
PLAN_WEIGHTS: dict[str, dict[str, float]] = {
    PLAN_SWING: {
        "l2_industry": 0.15,
        "l3_fundamental": 0.22,
        "l4_expectation": 0.23,
        "l5_timing": 0.28,
        "l6_event": 0.12,
    },
    PLAN_LONG: {
        "l2_industry": 0.15,
        "l3_fundamental": 0.40,
        "l4_expectation": 0.25,
        "l5_timing": 0.10,
        "l6_event": 0.10,
    },
    PLAN_EVENT: {
        "l2_industry": 0.10,
        "l3_fundamental": 0.15,
        "l4_expectation": 0.35,
        "l5_timing": 0.18,
        "l6_event": 0.22,
    },
}

VETO_LABELS = {
    "st_or_delist": "ST/*ST 或退市整理状态",
    "suspended_or_no_trade": "当日停牌或无成交",
    "illiquid_20d": "近20日日均成交额不足2500万元（流动性不足）",
    "young_listing": "上市未满60个自然日（次新波动未稳）",
    "event_critical_veto": "近期存在立案调查/退市风险警示/财务造假重大利空",
    "downtrend_knife": "深跌破位60日线且均线空头排列（波段防接飞刀）",
    "zombie_downtrend": "中长期均线持续下行压制（剔除阴跌僵尸股）",
    "earnings_shock": "近期发布亏损/大幅下滑业绩预警",
    "high_debt_loss": "高负债（>80%）叠加业绩亏损（长线排雷）",
}

_ST_RE = re.compile(r"(?:S\*ST|\*ST|SST|ST|退)", re.IGNORECASE)
_A_SHARE_RE = re.compile(r"^(?:6\d{5}\.SH|[03]\d{5}\.SZ|[489]\d{5}\.BJ)$")
MIN_AVG_AMOUNT_20_QIAN = 25_000.0   # 2500 万元（低于此直接排除）
ACTION_MIN_AMOUNT_20_QIAN = 45_000.0  # 4500 万元（低于此最高只到「中等」）
ACTION_MIN_L3_QUALITY = 48.0        # 基本面分低于 48 最高只到「中等」
SHRINKAGE_FULL_CONF_COVERAGE = 0.85
ACTION_MIN_COVERAGE = 0.65
LOT = 100
STAR_MIN_LOT = 200


def mv_bucket_of(total_mv_yi: float | None) -> str:
    """四档市值同温层分桶：消除小市值成交额因子跨生态位虹吸。"""
    if total_mv_yi is None or total_mv_yi < 50.0:
        return "small_micro"   # < 50 亿
    if total_mv_yi < 100.0:
        return "growth_mid"    # 50–100 亿
    if total_mv_yi < 300.0:
        return "mid_core"      # 100–300 亿
    return "mega_large"        # >= 300 亿


def compute_trend_pullback_score(
    *,
    close: float | None,
    ma20: float | None,
    ma60: float | None,
    ma60_slope: float | None,
    amt_ratio_5_20: float | None,
) -> float | None:
    """计算无市值偏见的自身趋势结构与缩量回踩企稳度（0 ~ 100）。

    高分画像：60日线走平向上 + 20日线站稳60日线（上升/筑底结构） + 股价温和回踩贴近20日线 + 近5日量能相对自身20日均量适度收缩洗盘。
    """
    if close is None or close <= 0 or ma20 is None or ma20 <= 0:
        return None

    # 1) 中期趋势结构健康度 (50%)
    if ma60 is not None and ma60 > 0:
        slope_ok = ma60_slope is None or ma60_slope >= -0.004
        if ma20 >= ma60 * 0.995 and close >= ma60 * 0.98 and slope_ok:
            trend_part = 90.0
        elif ma20 >= ma60 * 0.97 and close >= ma60 * 0.95:
            trend_part = 72.0
        elif close >= ma20:
            trend_part = 54.0
        else:
            trend_part = 24.0
    else:
        trend_part = 72.0 if close >= ma20 else 42.0

    # 2) 相对 20 日均线买点位置（不追暴涨透支、不接破位深跌）(30%)
    gap20 = close / ma20 - 1.0
    if -0.055 <= gap20 <= 0.045:
        pos_part = 90.0
    elif (0.045 < gap20 <= 0.10) or (-0.095 <= gap20 < -0.055):
        pos_part = 66.0
    elif 0.10 < gap20 <= 0.16:
        pos_part = 42.0
    else:
        pos_part = 22.0

    # 3) 相对自身 20 日均量的缩量洗盘/温和承接度 (20%)
    if amt_ratio_5_20 is None:
        vol_part = 65.0
    elif 0.48 <= amt_ratio_5_20 <= 0.92:
        vol_part = 90.0
    elif 0.92 < amt_ratio_5_20 <= 1.35:
        vol_part = 72.0
    elif 0.35 <= amt_ratio_5_20 < 0.48 or 1.35 < amt_ratio_5_20 <= 1.85:
        vol_part = 50.0
    else:
        vol_part = 28.0

    return round(0.50 * trend_part + 0.30 * pos_part + 0.20 * vol_part, 2)


def _compact_date(value: Any) -> str:
    raw = str(value or "").strip().replace("-", "")
    return raw if len(raw) == 8 and raw.isdigit() else ""


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _tick_down(price: float) -> float:
    return math.floor(price * 100.0 + 1e-9) / 100.0


def board_of(symbol: str) -> str:
    if symbol.startswith("688"):
        return "STAR"      # 科创板
    if symbol.startswith(("300", "301")):
        return "CHINEXT"   # 创业板
    if symbol.endswith(".BJ"):
        return "BSE"       # 北交所
    return "MAIN"          # 沪深主板


def _pct_ranks(values: dict[str, float | None], *, ascending: bool = True) -> dict[str, float]:
    """横截面百分位（0 ~ 100），缺失值不参与排名。"""
    clean = {k: v for k, v in values.items() if v is not None and math.isfinite(v)}
    if not clean:
        return {}
    if len(clean) == 1:
        only_key = next(iter(clean))
        return {only_key: 50.0}
    series = pd.Series(clean, dtype="float64")
    ranked = series.rank(pct=True, ascending=ascending) * 100.0
    return {str(k): round(float(val), 2) for k, val in ranked.items()}


def _industry_neutral_pct_ranks(
    values: dict[str, float | None],
    sym_industry: dict[str, str | None],
    *,
    ascending: bool = True,
    ind_weight: float = 0.65,
    min_peers: int = 8,
) -> dict[str, float]:
    """行业内百分位（65%）+ 全市场百分位（35%）融合排名。

    消除银行（PE≈5/PB≈0.5）或轻资产软件（毛利率>75%/低负债）跨行业直接比绝对估值与毛利时的结构性垄断偏差；
    同行业有效样本不足 min_peers 时自动退化为全市场百分位。
    """
    mkt_rank = _pct_ranks(values, ascending=ascending)
    if not mkt_rank:
        return {}
    by_ind: dict[str, dict[str, float | None]] = defaultdict(dict)
    for sym, val in values.items():
        if sym in mkt_rank:
            ind = sym_industry.get(sym)
            if ind:
                by_ind[ind][sym] = val
    ind_rank_all: dict[str, float] = {}
    for peer_vals in by_ind.values():
        if len(peer_vals) >= min_peers:
            ind_rank_all.update(_pct_ranks(peer_vals, ascending=ascending))

    mkt_w = 1.0 - ind_weight
    blended: dict[str, float] = {}
    for sym, mr in mkt_rank.items():
        if sym in ind_rank_all:
            blended[sym] = round(ind_weight * ind_rank_all[sym] + mkt_w * mr, 2)
        else:
            blended[sym] = mr
    return blended


def combine_layers_with_shrinkage(
    layer_scores: dict[str, float | None],
    *,
    plan_type: str,
    veto_codes: Sequence[str],
) -> dict[str, Any]:
    """核心数学合成器：缺值不进分母 + 贝叶斯向 50 分中性收缩 + Veto 一票否决门禁。"""
    weights = PLAN_WEIGHTS.get(plan_type, PLAN_WEIGHTS[PLAN_SWING])
    total_weight = sum(weights.values())
    avail_weight = 0.0
    weighted_sum = 0.0
    for key, w in weights.items():
        score = _finite(layer_scores.get(key))
        if score is not None:
            clamped = max(0.0, min(100.0, score))
            avail_weight += w
            weighted_sum += w * clamped

    coverage = (avail_weight / total_weight) if total_weight > 0 else 0.0
    raw_score = (weighted_sum / avail_weight) if avail_weight > 0 else 50.0
    conf = min(1.0, coverage / SHRINKAGE_FULL_CONF_COVERAGE) if SHRINKAGE_FULL_CONF_COVERAGE > 0 else 1.0
    total_score = round(conf * raw_score + (1.0 - conf) * 50.0, 2)

    vetoed = len(veto_codes) > 0
    if vetoed or total_score < 55.0:
        verdict = VERDICT_PASS
    elif total_score >= 70.0 and coverage >= ACTION_MIN_COVERAGE:
        verdict = VERDICT_ACTION
    else:
        verdict = VERDICT_WATCH

    return {
        "raw_score": round(raw_score, 2),
        "coverage_ratio": round(coverage, 4),
        "total_score": total_score,
        "vetoed": vetoed,
        "veto_codes": list(veto_codes),
        "verdict": verdict,
        "verdict_label": VERDICT_LABELS[verdict],
    }


def score_cross_section_inputs(
    items: Sequence[dict[str, Any]],
    *,
    plan_types: Sequence[str] = VALID_PLANS,
) -> dict[str, list[dict[str, Any]]]:
    """纯函数：对全市场候选特征截面一次性计算 L2–L6 各层客观分与三套策略结论。

    每个 item 字典包含：
      symbol, name, market, industry, list_status, list_date, trade_date,
      close, pct_chg, volume, avg_amount_20, ret_20, vol_20, ma20, ma60, atr14,
      pe_ttm, pb, total_mv_yi, turnover_rate,
      model_rank, buy_k, suggested_buy_price, stop_ref_price, buy_basis,
      fin (dict | None), events (list[dict])
    """
    if not items:
        return {p: [] for p in plan_types}

    # --- L2 行业顺风分：汇总各行业有效成分股 20 日均涨幅 + 当日上涨占比 + 成交热度 ---
    ind_members: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for it in items:
        ind = (str(it.get("industry") or "").strip())
        if ind and (it.get("volume") or 0) > 0 and it.get("close"):
            ind_members[ind].append(it)

    ind_ret20: dict[str, float | None] = {}
    ind_up_ratio: dict[str, float | None] = {}
    ind_amt: dict[str, float | None] = {}
    for ind, members in ind_members.items():
        if len(members) < 3:
            continue
        r20s = [m["ret_20"] for m in members if m.get("ret_20") is not None]
        pcts = [m["pct_chg"] for m in members if m.get("pct_chg") is not None]
        amts = [m["avg_amount_20"] for m in members if m.get("avg_amount_20") is not None]
        ind_ret20[ind] = sum(r20s) / len(r20s) if r20s else None
        ind_up_ratio[ind] = sum(1 for p in pcts if p > 0) / len(pcts) if pcts else None
        ind_amt[ind] = sum(amts) if amts else None

    r20_rank = _pct_ranks(ind_ret20, ascending=True)
    up_rank = _pct_ranks(ind_up_ratio, ascending=True)
    amt_rank = _pct_ranks(ind_amt, ascending=True)
    industry_score_map: dict[str, float] = {}
    for ind in ind_members:
        parts = []
        if ind in r20_rank:
            parts.append((0.55, r20_rank[ind]))
        if ind in up_rank:
            parts.append((0.30, up_rank[ind]))
        if ind in amt_rank:
            parts.append((0.15, amt_rank[ind]))
        if parts:
            w_sum = sum(w for w, _ in parts)
            industry_score_map[ind] = round(sum(w * s for w, s in parts) / w_sum, 2)

    # --- L3 基本面质量与估值横截面百分位 ---
    ep_vals: dict[str, float | None] = {}
    bp_vals: dict[str, float | None] = {}
    roe_vals: dict[str, float | None] = {}
    gm_vals: dict[str, float | None] = {}
    debt_vals: dict[str, float | None] = {}
    ocf_vals: dict[str, float | None] = {}
    # --- L4 预期差（业绩增速与二阶加速度）横截面百分位 ---
    sales_acc_vals: dict[str, float | None] = {}
    profit_acc_vals: dict[str, float | None] = {}
    profit_yoy_vals: dict[str, float | None] = {}
    # --- L5 备用低波回撤分位（仅当 model_rank 缺失时兜底） ---
    low_vol_vals: dict[str, float | None] = {}
    reversal_vals: dict[str, float | None] = {}

    sym_industry: dict[str, str | None] = {}
    for it in items:
        sym = it["symbol"]
        sym_industry[sym] = str(it.get("industry") or "").strip() or None
        pe = _finite(it.get("pe_ttm"))
        pb = _finite(it.get("pb"))
        if pe is not None and pe > 0:
            ep_vals[sym] = 1.0 / pe
        if pb is not None and pb > 0:
            bp_vals[sym] = 1.0 / pb

        fin = it.get("fin") or {}
        roe_vals[sym] = _finite(fin.get("roe"))
        gm_vals[sym] = _finite(fin.get("grossprofit_margin"))
        debt_vals[sym] = _finite(fin.get("debt_to_assets"))
        ocf_vals[sym] = _finite(fin.get("ocf_yoy"))

        sales_acc_vals[sym] = _finite(fin.get("tr_yoy_accel"))
        profit_acc_vals[sym] = _finite(fin.get("profit_yoy_accel"))
        profit_yoy_vals[sym] = (
            _finite(fin.get("q_profit_yoy"))
            if _finite(fin.get("q_profit_yoy")) is not None
            else _finite(fin.get("netprofit_yoy"))
        )

        low_vol_vals[sym] = _finite(it.get("vol_20"))
        reversal_vals[sym] = _finite(it.get("ret_20"))

    # L3 估值与盈利质地采用「65% 同行业百分位 + 35% 全市场百分位」，杜绝银行低 PE/PB 横扫全市场
    ep_rank = _industry_neutral_pct_ranks(ep_vals, sym_industry, ascending=True)
    bp_rank = _industry_neutral_pct_ranks(bp_vals, sym_industry, ascending=True)
    roe_rank = _industry_neutral_pct_ranks(roe_vals, sym_industry, ascending=True)
    gm_rank = _industry_neutral_pct_ranks(gm_vals, sym_industry, ascending=True)
    debt_rank = _industry_neutral_pct_ranks(debt_vals, sym_industry, ascending=False)  # 低负债更优
    ocf_rank = _industry_neutral_pct_ranks(ocf_vals, sym_industry, ascending=True)

    sales_acc_rank = _pct_ranks(sales_acc_vals, ascending=True)
    profit_acc_rank = _pct_ranks(profit_acc_vals, ascending=True)
    profit_yoy_rank = _pct_ranks(profit_yoy_vals, ascending=True)

    low_vol_rank = _pct_ranks(low_vol_vals, ascending=False)
    # A 股 20 日涨幅呈显著反转效应（IC = -0.056），适度回调优于高位暴涨
    rev_rank = _pct_ranks(reversal_vals, ascending=False)

    # --- L5 核心去偏：在四档同市值梯队内部独立对 model_rank 做百分位排名（消除微盘成交额虹吸） ---
    bucket_raw_ranks: dict[str, dict[str, float | None]] = defaultdict(dict)
    for it in items:
        sym = it["symbol"]
        mr = _finite(it.get("model_rank"))
        if mr is not None:
            bkt = mv_bucket_of(_finite(it.get("total_mv_yi")))
            bucket_raw_ranks[bkt][sym] = mr

    size_neutral_mrank_pct: dict[str, float] = {}
    for bkt, bkt_vals in bucket_raw_ranks.items():
        if len(bkt_vals) >= 15:
            size_neutral_mrank_pct.update(_pct_ranks(bkt_vals, ascending=True))
        else:
            # 样本极少（如单元测试夹具）时直接使用原分位 × 100
            for s, val in bkt_vals.items():
                if val is not None:
                    size_neutral_mrank_pct[s] = round(max(0.0, min(100.0, val * 100.0)), 2)

    results_by_plan: dict[str, list[dict[str, Any]]] = {p: [] for p in plan_types}

    for it in items:
        sym = it["symbol"]
        name = str(it.get("name") or sym)
        ind = str(it.get("industry") or "").strip() or None
        trade_date = str(it.get("trade_date") or "")
        close = _finite(it.get("close"))
        pct_chg = _finite(it.get("pct_chg"))
        pe = _finite(it.get("pe_ttm"))
        fin = it.get("fin") or {}
        events = it.get("events") or []

        # L2 得分
        l2_score = industry_score_map.get(ind) if ind else None

        # L3 得分（估值 + PIT 财务质量，缺值不进分母）
        l3_parts: list[tuple[float, float]] = []
        if sym in ep_rank:
            l3_parts.append((0.22, ep_rank[sym]))
        elif pe is not None and pe < 0:
            l3_parts.append((0.22, 18.0))  # 亏损股估值低分
        if sym in bp_rank:
            l3_parts.append((0.18, bp_rank[sym]))
        if sym in roe_rank:
            l3_parts.append((0.25, roe_rank[sym]))
        if sym in gm_rank:
            l3_parts.append((0.15, gm_rank[sym]))
        if sym in debt_rank:
            l3_parts.append((0.10, debt_rank[sym]))
        if sym in ocf_rank:
            l3_parts.append((0.10, ocf_rank[sym]))
        l3_score = (
            round(sum(w * s for w, s in l3_parts) / sum(w for w, _ in l3_parts), 2)
            if l3_parts
            else None
        )

        # L4 预期差得分（PIT 业绩增速/二阶导 + 公告后价格反应因子）
        l4_parts: list[tuple[float, float]] = []
        if sym in profit_acc_rank:
            l4_parts.append((0.40, profit_acc_rank[sym]))
        if sym in sales_acc_rank:
            l4_parts.append((0.30, sales_acc_rank[sym]))
        if sym in profit_yoy_rank:
            l4_parts.append((0.30, profit_yoy_rank[sym]))

        l4_base = (
            sum(w * s for w, s in l4_parts) / sum(w for w, _ in l4_parts)
            if l4_parts
            else None
        )
        # 检验近期（<=5自然日）公告的市场反应（图 220 精髓：利空不跌加分、利好暴跌扣分）
        reaction_delta = 0.0
        reaction_note = ""
        if events and pct_chg is not None:
            recent_ev = [e for e in events if e.get("ann_date", "") >= _shift_date_str(trade_date, -5)]
            has_pos = any(e.get("polarity") == "positive" for e in recent_ev)
            has_neg = any(e.get("polarity") == "negative" for e in recent_ev)
            if has_neg and pct_chg > 0.5:
                reaction_delta = 14.0
                reaction_note = "利空落定股价逆势收涨（预期出尽信号）"
            elif has_pos and pct_chg < -3.0:
                reaction_delta = -18.0
                reaction_note = "利好兑现后放量下跌（谨防买预期卖事实）"
            elif has_pos and 0.0 <= pct_chg <= 5.5:
                reaction_delta = 10.0
                reaction_note = "正面催化温和承接（未过热透支）"

        if l4_base is not None:
            l4_score: float | None = round(max(0.0, min(100.0, l4_base + reaction_delta)), 2)
        elif reaction_note:
            l4_score = round(max(0.0, min(100.0, 55.0 + reaction_delta)), 2)
        else:
            l4_score = None

        # L5 资金与入场时机得分（同市值桶中性化 model_rank 55% + 自身趋势缩量回踩结构 45%）
        m_rank = _finite(it.get("model_rank"))
        neutral_mr_score = size_neutral_mrank_pct.get(sym)
        pullback_score = compute_trend_pullback_score(
            close=close,
            ma20=_finite(it.get("ma20")),
            ma60=_finite(it.get("ma60")),
            ma60_slope=_finite(it.get("ma60_slope")),
            amt_ratio_5_20=_finite(it.get("amt_ratio_5_20")),
        )
        l5_source = "size_neutral_model_and_pullback"
        if neutral_mr_score is not None and pullback_score is not None:
            l5_score: float | None = round(0.55 * neutral_mr_score + 0.45 * pullback_score, 2)
        elif neutral_mr_score is not None:
            l5_source = "size_neutral_model_only"
            l5_score = neutral_mr_score
        elif pullback_score is not None and sym in low_vol_rank and sym in rev_rank:
            l5_source = "pullback_low_vol_fallback"
            l5_score = round(0.50 * pullback_score + 0.30 * low_vol_rank[sym] + 0.20 * rev_rank[sym], 2)
        elif sym in low_vol_rank and sym in rev_rank:
            l5_source = "low_vol_reversal_fallback"
            l5_score = round(0.55 * low_vol_rank[sym] + 0.45 * rev_rank[sym], 2)
        else:
            l5_score = None

        # L6 事件与窗口得分 + 公共否决信号识别
        has_critical_veto = any(e.get("polarity") == VETO_POLARITY for e in events)
        pos_events = [e for e in events if e.get("polarity") == "positive"]
        neg_events = [e for e in events if e.get("polarity") == "negative"]
        has_earnings_shock = any(
            e.get("event_type") == "业绩" and e.get("polarity") == "negative" for e in events
        )
        if has_critical_veto:
            l6_score: float | None = 0.0
        else:
            ev_score = 62.0 + min(28.0, 12.0 * len(pos_events)) - min(42.0, 16.0 * len(neg_events))
            l6_score = round(max(5.0, min(95.0, ev_score)), 2)

        # 公共基础否决项（所有 plan 共用）
        base_vetoes: list[str] = []
        if it.get("list_status") not in (None, "L") or _ST_RE.search(name):
            base_vetoes.append("st_or_delist")
        if not close or close <= 0 or (it.get("volume") is not None and float(it["volume"]) <= 0):
            base_vetoes.append("suspended_or_no_trade")
        avg_amt_20 = _finite(it.get("avg_amount_20"))
        if avg_amt_20 is not None and avg_amt_20 < MIN_AVG_AMOUNT_20_QIAN:
            base_vetoes.append("illiquid_20d")
        list_date = _compact_date(it.get("list_date"))
        if list_date and trade_date and list_date > _shift_date_str(trade_date, -60):
            base_vetoes.append("young_listing")
        if has_critical_veto:
            base_vetoes.append("event_critical_veto")

        ma20 = _finite(it.get("ma20"))
        ma60 = _finite(it.get("ma60"))
        ma60_slope = _finite(it.get("ma60_slope"))
        ret20 = _finite(it.get("ret_20"))
        debt_ratio = _finite(fin.get("debt_to_assets"))
        profit_yoy = profit_yoy_vals.get(sym)

        layer_map = {
            "l2_industry": l2_score,
            "l3_fundamental": l3_score,
            "l4_expectation": l4_score,
            "l5_timing": l5_score,
            "l6_event": l6_score,
        }

        for plan in plan_types:
            plan_vetoes = list(base_vetoes)
            if plan == PLAN_SWING:
                # 波段专属：1) 深跌破位 60 日均线防接飞刀；2) 60 日线持续下行且空头排列剔除阴跌僵尸股
                if (
                    close is not None
                    and ma60 is not None
                    and ma20 is not None
                    and close < ma60 * 0.92
                    and ma20 < ma60
                    and (ret20 is not None and ret20 < -0.12)
                ):
                    plan_vetoes.append("downtrend_knife")
                elif (
                    close is not None
                    and ma60 is not None
                    and ma20 is not None
                    and close < ma60 * 0.96
                    and ma20 < ma60 * 0.97
                    and (ma60_slope is not None and ma60_slope < -0.012)
                ):
                    plan_vetoes.append("zombie_downtrend")
                if has_earnings_shock:
                    plan_vetoes.append("earnings_shock")
            elif plan == PLAN_LONG:
                # 长线专属：资产负债率 >80% 且净利显著下滑或亏损
                if (
                    debt_ratio is not None
                    and debt_ratio > 80.0
                    and ((pe is not None and pe < 0) or (profit_yoy is not None and profit_yoy < -20.0))
                ):
                    plan_vetoes.append("high_debt_loss")

            merged = combine_layers_with_shrinkage(
                layer_map,
                plan_type=plan,
                veto_codes=plan_vetoes,
            )
            # 机构流动性与基本面底线门禁：日均成交不足 4500 万元或基本面质地 <48 分的冷门弱质票，最高只到「中等」，不进「高分」
            if merged["verdict"] == VERDICT_ACTION:
                if (avg_amt_20 is not None and avg_amt_20 < ACTION_MIN_AMOUNT_20_QIAN) or (
                    l3_score is not None and l3_score < ACTION_MIN_L3_QUALITY
                ):
                    merged["verdict"] = VERDICT_WATCH
                    merged["verdict_label"] = VERDICT_LABELS[VERDICT_WATCH]

            thesis = _build_objective_thesis(
                name=name,
                industry=ind,
                l2_score=l2_score,
                l3_score=l3_score,
                l4_score=l4_score,
                l5_score=l5_score,
                m_rank=m_rank,
                fin=fin,
                reaction_note=reaction_note,
                events=events,
            )

            evidence = {
                "l5_source": l5_source,
                "buy_basis": it.get("buy_basis"),
                "ma20": round(ma20, 2) if ma20 is not None else None,
                "ma60": round(ma60, 2) if ma60 is not None else None,
                "ret_20_pct": round(ret20 * 100.0, 2) if ret20 is not None else None,
                "avg_amount_20_yi": round(avg_amt_20 / 100_000.0, 3) if avg_amt_20 is not None else None,
                "fin_period": fin.get("end_date"),
                "fin_ann_date": fin.get("ann_date"),
                "roe": fin.get("roe"),
                "profit_yoy": profit_yoy,
                "profit_yoy_accel": fin.get("profit_yoy_accel"),
                "tr_yoy_accel": fin.get("tr_yoy_accel"),
                "reaction_note": reaction_note or None,
                "recent_events": events[:4],
                "veto_labels": [VETO_LABELS.get(code, code) for code in plan_vetoes],
            }

            results_by_plan[plan].append(
                {
                    "trade_date": trade_date,
                    "symbol": sym,
                    "name": name,
                    "market": it.get("market"),
                    "board": board_of(sym),
                    "industry": ind,
                    "plan_type": plan,
                    "total_score": merged["total_score"],
                    "raw_score": merged["raw_score"],
                    "coverage_ratio": merged["coverage_ratio"],
                    "verdict": merged["verdict"],
                    "verdict_label": merged["verdict_label"],
                    "vetoed": merged["vetoed"],
                    "veto_codes": merged["veto_codes"],
                    "veto_labels": evidence["veto_labels"],
                    "l2_industry_score": l2_score,
                    "l3_fundamental_score": l3_score,
                    "l4_expectation_score": l4_score,
                    "l5_timing_score": l5_score,
                    "l6_event_score": l6_score,
                    "model_rank": round(m_rank, 4) if m_rank is not None else None,
                    "buy_k": _finite(it.get("buy_k")),
                    "close_price": round(close, 2) if close is not None else None,
                    "pct_chg": round(pct_chg, 2) if pct_chg is not None else None,
                    "atr14": round(float(it["atr14"]), 3) if _finite(it.get("atr14")) is not None else None,
                    "pe_ttm": round(pe, 2) if pe is not None else None,
                    "pb": round(float(it["pb"]), 2) if _finite(it.get("pb")) is not None else None,
                    "total_mv_yi": round(float(it["total_mv_yi"]), 2) if _finite(it.get("total_mv_yi")) is not None else None,
                    "turnover_rate": round(float(it["turnover_rate"]), 2) if _finite(it.get("turnover_rate")) is not None else None,
                    "suggested_buy_price": it.get("suggested_buy_price"),
                    "stop_ref_price": it.get("stop_ref_price"),
                    "expectation_thesis": thesis,
                    "evidence": evidence,
                }
            )

    def _sort_key(row: dict[str, Any]) -> tuple[int, float, str]:
        return (
            0 if row["verdict"] == VERDICT_ACTION else (1 if row["verdict"] == VERDICT_WATCH else 2),
            -float(row["total_score"]),
            str(row["symbol"]),
        )

    apply_sector_diversity = len(items) >= 30
    for plan in plan_types:
        results_by_plan[plan].sort(key=_sort_key)
        if apply_sector_diversity:
            # 行业集中度软限（L1-L7 组合风控纪律）：同一行业前 2 只零衰减，第 3 只起阶梯扣分（-2.5/-5.0/-7.5/-9.0），
            # 防止单一板块（如银行/白酒/公用事业）霸榜前十，确保主流精选池呈现跨行业最优龙头。
            ind_seen: dict[tuple[str, str], int] = defaultdict(int)
            for row in results_by_plan[plan]:
                if row["vetoed"]:
                    continue
                ind = row.get("industry")
                if not ind:
                    continue
                mv = row.get("total_mv_yi")
                tier_key = "mainstream" if (mv is None or float(mv) >= 50.0) else "micro"
                seen = ind_seen[(tier_key, ind)]
                ind_seen[(tier_key, ind)] = seen + 1
                if seen >= 2:
                    penalty = round(min(9.0, 2.5 * (seen - 1)), 2)
                    new_score = round(max(0.0, float(row["total_score"]) - penalty), 2)
                    row["total_score"] = new_score
                    row["evidence"]["industry_crowding_penalty"] = penalty
                    if row["verdict"] == VERDICT_ACTION and new_score < 70.0:
                        row["verdict"] = VERDICT_WATCH
                        row["verdict_label"] = VERDICT_LABELS[VERDICT_WATCH]
                    elif row["verdict"] == VERDICT_WATCH and new_score < 55.0:
                        row["verdict"] = VERDICT_PASS
                        row["verdict_label"] = VERDICT_LABELS[VERDICT_PASS]
            results_by_plan[plan].sort(key=_sort_key)
    return results_by_plan


def _shift_date_str(compact_date: str, delta_days: int) -> str:
    try:
        dt = datetime.strptime(compact_date, "%Y%m%d").date()
        return (dt + timedelta(days=delta_days)).strftime("%Y%m%d")
    except ValueError:
        return compact_date


def _build_objective_thesis(
    *,
    name: str,
    industry: str | None,
    l2_score: float | None,
    l3_score: float | None,
    l4_score: float | None,
    l5_score: float | None,
    m_rank: float | None,
    fin: dict[str, Any],
    reaction_note: str,
    events: list[dict[str, Any]],
) -> str:
    """生成结构化客观预期差摘要（不捏造主观故事，全部基于可核验数值）。"""
    parts: list[str] = []
    if m_rank is not None:
        if m_rank >= 0.5:
            parts.append(f"量价模型排名前 {max(0.1, (1.0 - m_rank) * 100.0):.1f}%")
        else:
            parts.append(f"量价模型分位 {m_rank * 100.0:.1f}%（偏弱）")
    if industry and l2_score is not None:
        parts.append(f"所属「{industry}」行业景气分 {l2_score:.0f}")
    if fin.get("ann_date"):
        acc = fin.get("profit_yoy_accel")
        roe = fin.get("roe")
        fin_bits = [f"截至披露日 {fin['ann_date']}"]
        if roe is not None:
            fin_bits.append(f"ROE {float(roe):.1f}%")
        if acc is not None:
            fin_bits.append(f"净利增速环比{'提速' if float(acc) >= 0 else '放缓'} {float(acc):+.1f}pct")
        parts.append("，".join(fin_bits))
    if reaction_note:
        parts.append(reaction_note)
    elif events:
        top_ev = events[0]
        parts.append(f"近期事件：{top_ev.get('event_type')}（{top_ev.get('ann_date')}）")
    return "；".join(parts) if parts else f"{name} 按现有客观截面数据生成分层评估"


def compute_execution_levels(
    bars: list[Bar],
    *,
    symbol: str,
    name: str,
    buy_k: float | None,
    order_table: dict[str, Any] | None,
    has_signal: bool = True,
) -> dict[str, Any]:
    """执行层：买价和止损下限用操作计划同一套代码算（plans/quant_orders），页面和计划不会给出两个价。

    - 买价只来自买入模型（昨收 - buy_k 倍 ATR）。模型说不挂（has_signal 且 buy_k 为空）或当天没有信号，
      就不给买价；统计表的买价没跑赢简单基线（docs/quant-order-plan.md 第 0 节），不拿来兜底。
    - 止损只给下限：可以不设，设了不能比 3 倍 ATR 更近。回测里 1 到 2 倍 ATR 的止损把年化打成负数。
    """
    if not bars:
        return {
            "atr14": None,
            "ma20": None,
            "ma60": None,
            "ma60_slope": None,
            "ret_20": None,
            "vol_20": None,
            "suggested_buy_price": None,
            "stop_ref_price": None,
            "buy_basis": None,
        }
    adj = adjusted_bars(bars)
    closes = [b.close for b in adj]
    last_close = bars[-1].close
    atr_val = _atr(adj) if len(adj) >= 5 else (last_close * 0.025)
    ma20 = sum(closes[-20:]) / 20.0 if len(closes) >= 20 else None
    ma60 = sum(closes[-60:]) / 60.0 if len(closes) >= 60 else None
    ma60_prev5 = sum(closes[-65:-5]) / 60.0 if len(closes) >= 65 else None
    ma60_slope = (ma60 / ma60_prev5 - 1.0) if (ma60 and ma60_prev5 and ma60_prev5 > 0) else None
    ret_20 = (closes[-1] / closes[-21] - 1.0) if len(closes) >= 21 and closes[-21] > 0 else None

    vol_20: float | None = None
    if len(closes) >= 21:
        rets = [closes[i] / closes[i - 1] - 1.0 for i in range(len(closes) - 20, len(closes)) if closes[i - 1] > 0]
        if len(rets) >= 10:
            mean_r = sum(rets) / len(rets)
            vol_20 = math.sqrt(sum((r - mean_r) ** 2 for r in rets) / len(rets))

    limit_ratio = plan_limit_ratio(symbol, name)
    # 和操作计划同一个函数算昨收、ATR 和止损下限（plans/quant_orders.levels_for）
    levels = plan_orders(bars, order_table, limit_ratio=limit_ratio) if order_table is not None else None
    ref_close = levels.close if levels is not None else last_close
    ref_atr = levels.atr if levels is not None else atr_val

    suggested_buy: float | None = None
    if buy_k is not None and ref_atr > 0:
        suggested_buy = model_buy_price(ref_close, ref_atr, buy_k, limit_ratio=limit_ratio)
        buy_basis = (
            f"买入模型限价（昨收 - {buy_k:g} 倍 ATR）"
            if suggested_buy is not None
            else "买入模型的价低于跌停价，挂不上，不给买价"
        )
    elif has_signal:
        buy_basis = "买入模型今天判断不挂买单，不给买价"
    else:
        buy_basis = "今天没有买入模型信号，不给买价"

    stop_ref: float | None = None
    if levels is not None and levels.stop is not None:
        stop_ref = levels.stop.price
    elif ref_close > 0 and ref_atr > 0:
        stop_ref = max(_tick_down(ref_close - STOP_ATR * ref_atr), round(ref_close * (1.0 - limit_ratio), 2), 0.01)

    return {
        "atr14": atr_val,
        "ma20": ma20,
        "ma60": ma60,
        "ma60_slope": ma60_slope,
        "ret_20": ret_20,
        "vol_20": vol_20,
        "suggested_buy_price": suggested_buy,
        "stop_ref_price": stop_ref,
        "buy_basis": buy_basis,
    }


def compute_l7_position_sizing(
    *,
    symbol: str,
    entry_price: float | None,
    close: float | None,
    atr14: float | None,
    model_rank: float | None,
    capital: float,
    stock_count: int | None = None,
    held_shares: float = 0.0,
    no_buy_reason: str | None = None,
    allowed_boards: Iterable[str] | None = None,
) -> dict[str, Any]:
    """L7 股数：和操作计划同一条规则（shadow/rules.target_shares + plans/quant_sizes.budget_for）。

      目标股数 = min(风险预算 × 资金 ÷ 2 倍 ATR, 单只上限 × 资金 ÷ 收盘价) × (0.5 + 模型排名)
      风险预算 = 1% × max(1, 8 ÷ 持股只数)，单只上限 = max(25%, 1 ÷ 持股只数)，持股只数算上这一只

    计划里的资金只算已有持仓的市值；这里是新开仓，用账户预算代替。验证过的规则不开新仓，所以只作参考。
    已经持有的股票不在这里另算，以操作计划里的量化股数为准。不看 L1 仓位上限，它还没有验证。
    """
    from backend.app.plans.quant_sizes import TYPICAL_STOCKS, budget_for

    board = board_of(symbol)
    empty = {
        "board": board,
        "eligible": False,
        "advised_shares": 0,
        "advised_amount": 0.0,
        "position_pct": 0.0,
        "max_loss_amount": 0.0,
    }
    if allowed_boards is not None and board not in set(allowed_boards):
        return {**empty, "reason": f"账户未勾选或未开通 {board} 板块权限"}
    if held_shares > 0:
        return {**empty, "reason": f"这个账户已持有 {int(held_shares)} 股，加减多少以操作计划里的量化股数为准"}
    if not entry_price or entry_price <= 0:
        return {**empty, "reason": no_buy_reason or "没有买入模型给的买价，不给股数"}
    if capital <= 0 or not close or close <= 0 or not atr14 or atr14 <= 0:
        return {**empty, "reason": "缺少资金预算或 ATR，算不出股数"}

    count = stock_count if stock_count and stock_count > 0 else TYPICAL_STOCKS
    risk_budget, position_cap = budget_for(count)
    tilt = shadow_rules.rank_tilt(model_rank)
    raw = shadow_rules.target_shares(capital, close, atr14, risk_budget=risk_budget, position_cap=position_cap) * tilt
    min_lot = STAR_MIN_LOT if board == "STAR" else LOT
    shares = int(raw // LOT) * LOT
    if board == "STAR" and shares < STAR_MIN_LOT:
        shares = 0
    if shares <= 0:
        return {
            **empty,
            "entry_price": round(entry_price, 2),
            "min_lot_cost": round(min_lot * entry_price, 2),
            "reason": f"按规则算出来不到 {min_lot} 股（一手约 {min_lot * entry_price:,.0f} 元），不买",
        }

    amount = round(shares * entry_price, 2)
    return {
        **empty,
        "eligible": True,
        "advised_shares": shares,
        "advised_amount": amount,
        "position_pct": round(amount / capital, 4),
        # 不设止损，没有“最多亏多少”；这里是跌 2 倍 ATR 时大约亏的钱，规则就是按它定的风险预算
        "max_loss_amount": round(shares * shadow_rules.SIZING_ATR * atr14, 2),
        "risk_budget_pct": round(risk_budget, 4),
        "entry_price": round(entry_price, 2),
        "reason": (
            f"按持仓股数规则估算：每只风险预算 {risk_budget * 100:.1f}%、单只不超过 {position_cap * 100:.0f}%"
            f"（按 {count} 只持股），模型排名调整 ×{tilt:.2f}；新开仓没有验证过的股数规则，只作参考"
        ),
    }


def build_daily_selection_snapshots(
    db: Session,
    *,
    trade_date: str,
    plan_types: Sequence[str] = VALID_PLANS,
) -> dict[str, Any]:
    """预计算指定交易日的全市场 L1 环境 + L2–L6 分层选股截面并幂等写入数据库。"""
    target = _compact_date(trade_date)
    if not target:
        raise ValueError(f"无效交易日：{trade_date}")

    # 1. 读取候选 A 股目录
    instruments = [
        inst
        for inst in db.scalars(
            select(Instrument).where(Instrument.type == "stock").order_by(Instrument.symbol)
        ).all()
        if _A_SHARE_RE.fullmatch(inst.symbol)
        and (not _compact_date(inst.list_date) or _compact_date(inst.list_date) <= target)
    ]
    if not instruments:
        regime = compute_and_store_market_regime(db, target)
        return {"trade_date": target, "regime": regime, "rows_written": 0, "universe": 0}

    symbols = [inst.symbol for inst in instruments]
    start_window = _shift_date_str(target, -150)

    # 2. 单次分批索引扫描读取最近 ~65–80 个交易日的 K 线（免 SQL filesort，且同时供给 L1 市场宽度与 L5/执行层）
    bars_by_sym: dict[str, list[Bar]] = defaultdict(list)
    latest_bar_meta: dict[str, dict[str, Any]] = {}
    amounts_20_by_sym: dict[str, list[float]] = defaultdict(list)
    daily_amt_sum_qian: dict[str, float] = defaultdict(float)
    up_count = 0
    down_count = 0

    for idx in range(0, len(symbols), 300):
        batch = symbols[idx : idx + 300]
        rows = db.execute(
            select(
                DailyBar.symbol,
                DailyBar.trade_date,
                DailyBar.high,
                DailyBar.low,
                DailyBar.close,
                DailyBar.pre_close,
                DailyBar.change_pct,
                DailyBar.volume,
                DailyBar.amount,
            ).where(
                DailyBar.symbol.in_(batch),
                DailyBar.trade_date >= start_window,
                DailyBar.trade_date <= target,
                DailyBar.source == "tushare",
                DailyBar.close.is_not(None),
                DailyBar.close > 0,
            )
        ).all()
        chosen: dict[tuple[str, str], Any] = {}
        for r in rows:
            chosen[(str(r.symbol), str(r.trade_date))] = r
        for (sym, day), r in sorted(chosen.items()):
            bars_by_sym[sym].append(
                Bar(
                    trade_date=day,
                    high=float(r.high if r.high is not None else r.close),
                    low=float(r.low if r.low is not None else r.close),
                    close=float(r.close),
                    pre_close=float(r.pre_close) if r.pre_close is not None else None,
                )
            )
            if r.amount is not None and math.isfinite(float(r.amount)) and float(r.amount) >= 0:
                amt_val = float(r.amount)
                amounts_20_by_sym[sym].append(amt_val)
                daily_amt_sum_qian[day] += amt_val
            if day == target:
                pct = _finite(r.change_pct)
                if pct is None and r.pre_close and float(r.pre_close) > 0:
                    pct = (float(r.close) / float(r.pre_close) - 1.0) * 100.0
                vol_val = _finite(r.volume)
                latest_bar_meta[sym] = {
                    "close": float(r.close),
                    "pct_chg": pct,
                    "volume": vol_val,
                }
                if (vol_val is None or vol_val > 0) and pct is not None:
                    if pct > 0:
                        up_count += 1
                    elif pct < 0:
                        down_count += 1

    # 3. 复用已加载的 bars_by_sym 与每日总成交额直接计算 L1 市场环境（零额外 K 线 SQL）
    regime = compute_and_store_market_regime(
        db,
        target,
        preloaded_bars=bars_by_sym,
        preloaded_up_down=(up_count, down_count),
        preloaded_daily_amounts_yi={d: v / 100_000.0 for d, v in daily_amt_sum_qian.items() if v > 0},
    )

    # 4. 单条索引查询加载当日全市场 StockDailyBasic（走 ix_stock_daily_basic_trade_date）
    basics_by_sym: dict[str, StockDailyBasic] = {
        b.symbol: b
        for b in db.scalars(
            select(StockDailyBasic).where(StockDailyBasic.trade_date == target)
        ).all()
    }

    # 读取或现场计算 StockModelSignal（7特征验证过的 model_rank 与 buy_k）
    model_signals: dict[str, tuple[float | None, float | None]] = {
        row.symbol: (_finite(row.model_rank), _finite(row.buy_k))
        for row in db.execute(
            select(StockModelSignal.symbol, StockModelSignal.model_rank, StockModelSignal.buy_k).where(
                StockModelSignal.trade_date == target
            )
        ).all()
    }
    if not model_signals:
        try:
            from backend.app.shadow.rank_signal import latest_signals, save_signals

            live = latest_signals(db, target)
            if live.ranks:
                if live.complete:
                    save_signals(db, target, live)
                for s, rk in live.ranks.items():
                    model_signals[s] = (_finite(rk), _finite(live.buy_k.get(s)))
        except Exception as sig_exc:  # noqa: BLE001
            logger.warning("分层选股实时计算 model_rank 跳过 trade_date=%s: %s", target, str(sig_exc)[:180])

    pit_fin = load_pit_financials(db, as_of_date=target)
    pit_events = load_pit_event_signals(db, as_of_date=target)
    order_table = load_order_table()

    cross_items: list[dict[str, Any]] = []
    for inst in instruments:
        sym = inst.symbol
        sym_bars = bars_by_sym.get(sym, [])
        bar_today = latest_bar_meta.get(sym)
        if not bar_today and not sym_bars:
            # 退市且无近期日线的历史壳不再塞入每日活跃快照，节省空间
            if inst.list_status == "D":
                continue
        m_rank, buy_k = model_signals.get(sym, (None, None))
        exec_info = compute_execution_levels(
            sym_bars,
            symbol=sym,
            name=inst.name,
            buy_k=buy_k,
            order_table=order_table,
            has_signal=sym in model_signals,
        )
        amts = amounts_20_by_sym.get(sym, [])
        avg_amt_20 = (sum(amts[-20:]) / len(amts[-20:])) if amts else None
        amt_ratio_5_20 = (
            (sum(amts[-5:]) / len(amts[-5:])) / avg_amt_20
            if len(amts) >= 10 and avg_amt_20 and avg_amt_20 > 0
            else None
        )
        basic = basics_by_sym.get(sym)
        total_mv_yi = (
            float(basic.total_mv) / 10_000.0
            if basic and _finite(basic.total_mv) is not None and float(basic.total_mv) > 0
            else None
        )

        cross_items.append(
            {
                "symbol": sym,
                "name": inst.name,
                "market": inst.market,
                "industry": (inst.industry or "").strip() or None,
                "list_status": inst.list_status,
                "list_date": inst.list_date,
                "trade_date": target,
                "close": bar_today["close"] if bar_today else None,
                "pct_chg": bar_today["pct_chg"] if bar_today else None,
                "volume": bar_today["volume"] if bar_today else 0.0,
                "avg_amount_20": avg_amt_20,
                "amt_ratio_5_20": amt_ratio_5_20,
                "ret_20": exec_info["ret_20"],
                "vol_20": exec_info["vol_20"],
                "ma20": exec_info["ma20"],
                "ma60": exec_info["ma60"],
                "ma60_slope": exec_info["ma60_slope"],
                "atr14": exec_info["atr14"],
                "pe_ttm": _finite(basic.pe_ttm) if basic else None,
                "pb": _finite(basic.pb) if basic else None,
                "total_mv_yi": total_mv_yi,
                "turnover_rate": _finite(basic.turnover_rate) if basic else None,
                "model_rank": m_rank,
                "buy_k": buy_k,
                "suggested_buy_price": exec_info["suggested_buy_price"],
                "stop_ref_price": exec_info["stop_ref_price"],
                "buy_basis": exec_info["buy_basis"],
                "fin": pit_fin.get(sym),
                "events": pit_events.get(sym, []),
            }
        )

    scored_by_plan = score_cross_section_inputs(cross_items, plan_types=plan_types)

    # 4. 幂等分批批量写入当日这些 plan_type 的快照行（避免逐行 ORM INSERT 往返）
    db.execute(
        delete(StockSelectionDailySnapshot).where(
            StockSelectionDailySnapshot.trade_date == target,
            StockSelectionDailySnapshot.plan_type.in_(list(plan_types)),
        )
    )
    payload_dicts: list[dict[str, Any]] = []
    for plan, scored_rows in scored_by_plan.items():
        for item in scored_rows:
            payload_dicts.append(
                {
                    "trade_date": target,
                    "symbol": item["symbol"],
                    "name": item["name"],
                    "market": item["market"],
                    "industry": item["industry"],
                    "plan_type": plan,
                    "total_score": item["total_score"],
                    "raw_score": item["raw_score"],
                    "coverage_ratio": item["coverage_ratio"],
                    "verdict": item["verdict"],
                    "vetoed": item["vetoed"],
                    "veto_codes_json": json.dumps(item["veto_codes"], ensure_ascii=False),
                    "l2_industry_score": item["l2_industry_score"],
                    "l3_fundamental_score": item["l3_fundamental_score"],
                    "l4_expectation_score": item["l4_expectation_score"],
                    "l5_timing_score": item["l5_timing_score"],
                    "l6_event_score": item["l6_event_score"],
                    "model_rank": item["model_rank"],
                    "buy_k": item["buy_k"],
                    "close_price": item["close_price"],
                    "pct_chg": item["pct_chg"],
                    "atr14": item["atr14"],
                    "pe_ttm": item["pe_ttm"],
                    "pb": item["pb"],
                    "total_mv_yi": item["total_mv_yi"],
                    "turnover_rate": item["turnover_rate"],
                    "suggested_buy_price": item["suggested_buy_price"],
                    "stop_ref_price": item["stop_ref_price"],
                    "expectation_thesis": item["expectation_thesis"],
                    "evidence_json": json.dumps(item["evidence"], ensure_ascii=False),
                }
            )
    snap_table = StockSelectionDailySnapshot.__table__
    for chunk_start in range(0, len(payload_dicts), 250):
        db.execute(insert(snap_table).values(payload_dicts[chunk_start : chunk_start + 250]))
    db.flush()

    action_counts = {
        plan: sum(1 for r in rows if r["verdict"] == VERDICT_ACTION)
        for plan, rows in scored_by_plan.items()
    }
    return {
        "trade_date": target,
        "regime": regime,
        "universe": len(cross_items),
        "rows_written": len(payload_dicts),
        "action_counts": action_counts,
    }


def _snapshot_row_to_dict(row: StockSelectionDailySnapshot) -> dict[str, Any]:
    try:
        veto_codes = json.loads(row.veto_codes_json) if row.veto_codes_json else []
    except ValueError:
        veto_codes = []
    try:
        evidence = json.loads(row.evidence_json) if row.evidence_json else {}
    except ValueError:
        evidence = {}
    return {
        "trade_date": row.trade_date,
        "symbol": row.symbol,
        "name": row.name,
        "market": row.market,
        "board": board_of(row.symbol),
        "industry": row.industry,
        "plan_type": row.plan_type,
        "plan_label": PLAN_LABELS.get(row.plan_type, row.plan_type),
        "total_score": row.total_score,
        "raw_score": row.raw_score,
        "coverage_ratio": row.coverage_ratio,
        "verdict": row.verdict,
        "verdict_label": VERDICT_LABELS.get(row.verdict, row.verdict),
        "vetoed": bool(row.vetoed),
        "veto_codes": veto_codes,
        "veto_labels": [VETO_LABELS.get(c, c) for c in veto_codes],
        "layers": {
            "l2_industry": row.l2_industry_score,
            "l3_fundamental": row.l3_fundamental_score,
            "l4_expectation": row.l4_expectation_score,
            "l5_timing": row.l5_timing_score,
            "l6_event": row.l6_event_score,
        },
        "model_rank": row.model_rank,
        "buy_k": row.buy_k,
        "close_price": row.close_price,
        "pct_chg": row.pct_chg,
        "atr14": row.atr14,
        "pe_ttm": row.pe_ttm,
        "pb": row.pb,
        "total_mv_yi": row.total_mv_yi,
        "turnover_rate": row.turnover_rate,
        "suggested_buy_price": row.suggested_buy_price,
        "stop_ref_price": row.stop_ref_price,
        "expectation_thesis": row.expectation_thesis,
        "evidence": evidence,
    }


def _resolve_latest_trade_date(db: Session, requested: str | None = None) -> str | None:
    if requested:
        compact = _compact_date(requested)
        if compact:
            return compact
    snap_latest = db.scalar(select(func.max(StockSelectionDailySnapshot.trade_date)))
    bar_latest = db.scalar(
        select(func.max(StockDailyBasic.trade_date)).where(
            StockDailyBasic.trade_date <= date.today().strftime("%Y%m%d"),
        )
    )
    if not bar_latest:
        sample_sym = db.scalar(select(Instrument.symbol).where(Instrument.type == "stock").limit(1))
        if sample_sym:
            bar_latest = db.scalar(
                select(func.max(DailyBar.trade_date)).where(
                    DailyBar.symbol == sample_sym,
                    DailyBar.trade_date <= date.today().strftime("%Y%m%d"),
                )
            )
    if bar_latest and (not snap_latest or str(bar_latest) > str(snap_latest)):
        return str(bar_latest)
    return str(snap_latest or bar_latest or "") or None


def resolve_user_account_budget(
    db: Session,
    *,
    owner_user_id: int | None,
    account_id: int | None = None,
    fallback_capital: float | None = None,
) -> tuple[float, dict[str, float]]:
    """账户预算和这个账户现有的持仓（代码 -> 股数），L7 用来定持股只数、跳过已持有的股票。"""
    if owner_user_id is not None and account_id is not None and account_id > 0:
        acct = db.scalar(
            select(PositionAccount).where(
                PositionAccount.id == account_id,
                PositionAccount.owner_user_id == owner_user_id,
            )
        )
        if acct is not None:
            positions = db.scalars(
                select(Position).where(
                    Position.owner_user_id == owner_user_id,
                    Position.account_id == account_id,
                    Position.status == "open",
                    Position.quantity > 0,
                )
            ).all()
            invested = sum(
                float(p.quantity) * float(p.last_price or p.cost_price or 0.0) for p in positions
            )
            holdings: dict[str, float] = {}
            for p in positions:
                holdings[str(p.symbol)] = holdings.get(str(p.symbol), 0.0) + float(p.quantity)
            cash = float(getattr(acct, "available_cash", 0.0) or 0.0)
            total_cap = fallback_capital if (fallback_capital and fallback_capital > 0) else max(invested + cash, invested, 100_000.0)
            return total_cap, holdings
    cap = float(fallback_capital) if (fallback_capital and fallback_capital > 0) else 200_000.0
    return cap, {}


def _l7_for_row(
    row: StockSelectionDailySnapshot,
    *,
    capital: float,
    holdings: dict[str, float],
    account_selected: bool,
    allowed_boards: Iterable[str] | None = None,
) -> dict[str, Any]:
    evidence = json.loads(row.evidence_json) if row.evidence_json else {}
    held = holdings.get(row.symbol, 0.0)
    return compute_l7_position_sizing(
        symbol=row.symbol,
        entry_price=row.suggested_buy_price,
        close=row.close_price,
        atr14=row.atr14,
        model_rank=row.model_rank,
        capital=capital,
        stock_count=(len(holdings) + (0 if held > 0 else 1)) if account_selected else None,
        held_shares=held,
        no_buy_reason=evidence.get("buy_basis") if isinstance(evidence, dict) else None,
        allowed_boards=allowed_boards,
    )


def query_layered_selection(
    *,
    plan_type: str = PLAN_SWING,
    trade_date: str | None = None,
    verdict_filter: str | None = None,
    industry: str | None = None,
    market: str | None = None,
    boards: Sequence[str] | None = None,
    keyword: str | None = None,
    price_max: float | None = None,
    pe_max: float | None = None,
    total_mv_min: float | None = None,
    total_mv_max: float | None = None,
    capital: float | None = None,
    account_id: int | None = None,
    owner_user_id: int | None = None,
    limit: int = 50,
    auto_build_if_missing: bool = True,
) -> dict[str, Any]:
    """全站统一分层选股高速查询入口（优先读预计算截面宽表，<50ms 返回）。"""
    if plan_type not in VALID_PLANS:
        raise ValueError(f"不支持的选股周期档位：{plan_type}，可选 {VALID_PLANS}")

    with SessionLocal() as db:
        target = _resolve_latest_trade_date(db, trade_date)
        if not target:
            regime = get_latest_market_regime(compute_if_missing=False)
            return {
                "trade_date": None,
                "plan_type": plan_type,
                "plan_label": PLAN_LABELS[plan_type],
                "weights": PLAN_WEIGHTS[plan_type],
                "regime": regime,
                "summary": {"total": 0, "action": 0, "watch": 0, "pass": 0},
                "items": [],
            }

        has_rows = db.scalar(
            select(func.count(StockSelectionDailySnapshot.id)).where(
                StockSelectionDailySnapshot.trade_date == target,
                StockSelectionDailySnapshot.plan_type == plan_type,
            )
        )
        if not has_rows and auto_build_if_missing:
            build_daily_selection_snapshots(db, trade_date=target, plan_types=VALID_PLANS)
            db.commit()

        regime = get_latest_market_regime(trade_date=target, compute_if_missing=True)
        resolved_cap, holdings = resolve_user_account_budget(
            db,
            owner_user_id=owner_user_id,
            account_id=account_id,
            fallback_capital=capital,
        )
        account_selected = bool(owner_user_id is not None and account_id)

        allowed_board_set = {str(b).upper() for b in boards} if boards else None
        conditions = [
            StockSelectionDailySnapshot.trade_date == target,
            StockSelectionDailySnapshot.plan_type == plan_type,
        ]
        if industry:
            conditions.append(StockSelectionDailySnapshot.industry == industry.strip())
        if market:
            conditions.append(StockSelectionDailySnapshot.market == market.strip().upper())
        if keyword:
            kw = f"%{keyword.strip()}%"
            conditions.append(
                or_(
                    StockSelectionDailySnapshot.symbol.ilike(kw),
                    StockSelectionDailySnapshot.name.ilike(kw),
                )
            )
        if price_max is not None:
            conditions.append(StockSelectionDailySnapshot.close_price <= float(price_max))
        if pe_max is not None:
            conditions.append(StockSelectionDailySnapshot.pe_ttm.is_not(None))
            conditions.append(StockSelectionDailySnapshot.pe_ttm > 0)
            conditions.append(StockSelectionDailySnapshot.pe_ttm <= float(pe_max))
        if total_mv_min is not None:
            conditions.append(StockSelectionDailySnapshot.total_mv_yi >= float(total_mv_min))
        if total_mv_max is not None:
            conditions.append(StockSelectionDailySnapshot.total_mv_yi <= float(total_mv_max))
        if allowed_board_set and len(allowed_board_set) < 4:
            board_clauses = []
            if "STAR" in allowed_board_set:
                board_clauses.append(StockSelectionDailySnapshot.symbol.like("688%"))
            if "CHINEXT" in allowed_board_set:
                board_clauses.append(
                    or_(
                        StockSelectionDailySnapshot.symbol.like("300%"),
                        StockSelectionDailySnapshot.symbol.like("301%"),
                    )
                )
            if "BSE" in allowed_board_set:
                board_clauses.append(StockSelectionDailySnapshot.symbol.like("%.BJ"))
            if "MAIN" in allowed_board_set:
                board_clauses.append(
                    not_(
                        or_(
                            StockSelectionDailySnapshot.symbol.like("688%"),
                            StockSelectionDailySnapshot.symbol.like("300%"),
                            StockSelectionDailySnapshot.symbol.like("301%"),
                            StockSelectionDailySnapshot.symbol.like("%.BJ"),
                        )
                    )
                )
            if board_clauses:
                conditions.append(or_(*board_clauses))

        # 1) SQL 端聚合三档结论数量（仅传输 3 行计数，免全表拉回 5500 行 LONGTEXT）
        counts = {VERDICT_ACTION: 0, VERDICT_WATCH: 0, VERDICT_PASS: 0}
        for v_key, cnt in db.execute(
            select(StockSelectionDailySnapshot.verdict, func.count(StockSelectionDailySnapshot.id))
            .where(*conditions)
            .group_by(StockSelectionDailySnapshot.verdict)
        ).all():
            if str(v_key) in counts:
                counts[str(v_key)] = int(cnt or 0)

        # 2) SQL 端按索引排序并只取前 limit 行
        row_conditions = list(conditions)
        if verdict_filter and verdict_filter in counts:
            row_conditions.append(StockSelectionDailySnapshot.verdict == verdict_filter)

        verdict_rank_expr = case(
            (StockSelectionDailySnapshot.verdict == VERDICT_ACTION, 0),
            (StockSelectionDailySnapshot.verdict == VERDICT_WATCH, 1),
            else_=2,
        )
        safe_limit = max(1, min(int(limit), 200))
        top_rows = list(
            db.scalars(
                select(StockSelectionDailySnapshot)
                .where(*row_conditions)
                .order_by(
                    verdict_rank_expr,
                    StockSelectionDailySnapshot.total_score.desc(),
                    StockSelectionDailySnapshot.symbol,
                )
                .limit(safe_limit)
            ).all()
        )

    total_matched = sum(counts.values())

    items: list[dict[str, Any]] = []
    for r in top_rows:
        d = _snapshot_row_to_dict(r)
        d["l7_risk"] = _l7_for_row(
            r,
            capital=resolved_cap,
            holdings=holdings,
            account_selected=account_selected,
            allowed_boards=allowed_board_set,
        )
        items.append(d)

    return {
        "trade_date": target,
        "plan_type": plan_type,
        "plan_label": PLAN_LABELS[plan_type],
        "weights": PLAN_WEIGHTS[plan_type],
        "regime": regime,
        "account_context": {
            "capital": resolved_cap,
            "holding_count": len(holdings) if account_selected else None,
        },
        "summary": {
            "total": total_matched,
            "action": counts[VERDICT_ACTION],
            "watch": counts[VERDICT_WATCH],
            "pass": counts[VERDICT_PASS],
        },
        "items": items,
    }


def inspect_single_stock_layered(
    symbol: str,
    *,
    trade_date: str | None = None,
    capital: float | None = None,
    account_id: int | None = None,
    owner_user_id: int | None = None,
) -> dict[str, Any]:
    """单票 L1–L7 深度体检：同时输出该标的在波段/长线/事件三档策略下的全栈证据与 L7 仓位方案。"""
    from backend.app.market.stock_tools import normalize_a_share_symbol

    norm_sym = normalize_a_share_symbol(str(symbol or "").strip())
    if not _A_SHARE_RE.fullmatch(norm_sym):
        raise ValueError(f"无法识别 A 股代码：{symbol}")

    with SessionLocal() as db:
        target = _resolve_latest_trade_date(db, trade_date)
        if not target:
            raise ValueError("本地尚无可用行情截面，请先同步市场行情")

        rows = db.scalars(
            select(StockSelectionDailySnapshot).where(
                StockSelectionDailySnapshot.trade_date == target,
                StockSelectionDailySnapshot.symbol == norm_sym,
            )
        ).all()
        if not rows:
            build_daily_selection_snapshots(db, trade_date=target, plan_types=VALID_PLANS)
            db.commit()
            rows = db.scalars(
                select(StockSelectionDailySnapshot).where(
                    StockSelectionDailySnapshot.trade_date == target,
                    StockSelectionDailySnapshot.symbol == norm_sym,
                )
            ).all()
        if not rows:
            raise ValueError(f"未在交易日 {target} 的全市场目录中找到标的 {norm_sym}")

        regime = get_latest_market_regime(trade_date=target, compute_if_missing=True)
        resolved_cap, holdings = resolve_user_account_budget(
            db,
            owner_user_id=owner_user_id,
            account_id=account_id,
            fallback_capital=capital,
        )
    account_selected = bool(owner_user_id is not None and account_id)

    by_plan: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = _snapshot_row_to_dict(r)
        d["l7_risk"] = _l7_for_row(r, capital=resolved_cap, holdings=holdings, account_selected=account_selected)
        by_plan[r.plan_type] = d

    primary = by_plan.get(PLAN_SWING) or next(iter(by_plan.values()))
    return {
        "trade_date": target,
        "symbol": norm_sym,
        "name": primary["name"],
        "industry": primary["industry"],
        "board": primary["board"],
        "close_price": primary["close_price"],
        "pct_chg": primary["pct_chg"],
        "suggested_buy_price": primary["suggested_buy_price"],
        "stop_ref_price": primary["stop_ref_price"],
        "regime": regime,
        "plans": by_plan,
        "l7_risk": primary["l7_risk"],
    }
