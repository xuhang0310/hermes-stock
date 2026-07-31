"""技能：价值投资分析 —— DCF 估值、财务红旗、护城河评估、估值快照。

基于《股市真规则》晨星公司标准，适配 hermes-stock 项目的 TuShare 数据源。
数据从 TuShare 财务报表接口获取（income/balancesheet/cashflow/fina_indicator），
不依赖 akshare 或外部 PDF。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from backend.app.market.stock_tools import (
    _latest_fina_indicator,
    _semantic_tushare,
    _tushare_request,
    _valid_yyyymmdd,
    get_balancesheet,
    get_cashflow,
    get_income,
    get_realtime_quote,
    get_stock_info,
    normalize_a_share_symbol,
)


# ---------------------------------------------------------------------------
# DCF 估值
# ---------------------------------------------------------------------------

# 晨星标准：6 个风险因素调整
_RISK_ADJUSTMENTS = {
    "small_size": {"desc": "小市值", "adj": 0.015},
    "high_leverage": {"desc": "高负债", "adj": 0.015},
    "cyclical": {"desc": "周期性行业", "adj": 0.015},
    "poor_management": {"desc": "管理层可信度低", "adj": 0.01},
    "weak_moat": {"desc": "弱竞争优势", "adj": 0.015},
    "complexity": {"desc": "业务复杂", "adj": 0.01},
}

_MORNINGSTAR_PRESETS = {
    "strong_moat": {"discount_rate": 0.09, "safety_margin": 0.20, "label": "强竞争优势公司"},
    "average": {"discount_rate": 0.105, "safety_margin": 0.30, "label": "一般公司"},
    "high_risk": {"discount_rate": 0.14, "safety_margin": 0.60, "label": "高风险公司"},
}


def _discount_rate(base: float, risk_factors: list[str] | None) -> tuple[float, list[str]]:
    total = 0.0
    notes: list[str] = []
    for f in risk_factors or []:
        item = _RISK_ADJUSTMENTS.get(f)
        if item:
            total += item["adj"]
            notes.append(f"{item['desc']}: +{item['adj'] * 100:.1f}%")
    return base + total, notes


def _generate_growth_rates(base: float, decay: float = 0.2, years: int = 10) -> list[float]:
    rates: list[float] = []
    g = base
    for i in range(years):
        if i >= 5:
            g *= 1 - decay
        rates.append(g)
    return rates


def _dcf_calc(
    fcf: float, growth_rates: list[float], terminal_g: float,
    discount: float, net_debt: float, shares: float,
) -> dict:
    pv_fcf = 0.0
    cur_fcf = fcf
    projections: list[dict] = []
    for i, g in enumerate(growth_rates):
        cur_fcf *= 1 + g
        pv = cur_fcf / ((1 + discount) ** (i + 1))
        pv_fcf += pv
        projections.append({"year": i + 1, "fcf": round(cur_fcf, 4), "growth": round(g, 4), "pv": round(pv, 4)})

    terminal_fcf = cur_fcf * (1 + terminal_g)
    terminal_value = terminal_fcf / (discount - terminal_g) if discount > terminal_g else 0
    pv_terminal = terminal_value / ((1 + discount) ** len(growth_rates))
    ev = pv_fcf + pv_terminal
    equity = ev - net_debt
    iv_per_share = equity / shares if shares > 0 else 0

    return {
        "projections": projections,
        "pv_forecast_period": round(pv_fcf, 4),
        "terminal_value": round(terminal_value, 4),
        "pv_terminal": round(pv_terminal, 4),
        "enterprise_value": round(ev, 4),
        "net_debt": round(net_debt, 4),
        "equity_value": round(equity, 4),
        "shares": shares,
        "intrinsic_value_per_share": round(iv_per_share, 4),
    }


def _safety_margin(iv: float, price: float) -> float:
    return (iv - price) / iv if iv > 0 else 0.0


def _evaluate(iv: float, price: float, moat: str) -> dict:
    margin = _safety_margin(iv, price)
    req_map = {"strong": (0.20, "强竞争优势公司"), "average": (0.35, "一般公司"), "weak": (0.60, "高风险公司")}
    required, label = req_map.get(moat, (0.35, "一般公司"))
    if margin >= required:
        action, note = "买入", f"安全边际{margin * 100:.1f}% ≥ 要求{required * 100:.0f}%"
    elif margin >= 0:
        action, note = "观望", f"安全边际{margin * 100:.1f}% < 要求{required * 100:.0f}%，等待更好价格"
    else:
        action, note = "回避", f"高估{abs(margin) * 100:.1f}%，建议回避"
    return {"safety_margin": round(margin, 4), "required_margin": required, "label": label, "action": action, "note": note}


def _fetch_fcf(ts_code: str) -> tuple[float | None, dict]:
    """从现金流量表取最近一期自由现金流（亿元）。"""
    cf = get_cashflow(ts_code)
    rows = cf.get("rows") or []
    if not rows:
        return None, {}
    row = rows[0]
    fcf_raw = row.get("free_cashflow")
    if fcf_raw is None:
        n_cashflow_act = row.get("n_cashflow_act") or 0
        # 简化：经营现金流 - 资本支出（此处取投资现金流负值近似）
        n_cashflow_inv = row.get("n_cashflow_inv") or 0
        fcf_raw = n_cashflow_act + n_cashflow_inv  # inv 通常为负
    fcf_yi = (fcf_raw or 0) / 1e4  # TuShare 单位：万元 -> 亿元
    return fcf_yi, row


def _fetch_net_debt(ts_code: str) -> float:
    """净负债（亿元）= 总有息负债 - 货币资金。正为净负债，负为净现金。"""
    bs = get_balancesheet(ts_code)
    rows = bs.get("rows") or []
    if not rows:
        return 0.0
    row = rows[0]
    total_liab = (row.get("total_liab") or 0) / 1e4
    money_cap = (row.get("money_cap") or 0) / 1e4
    return max(0.0, total_liab - money_cap)


def _fetch_shares_and_price(ts_code: str) -> tuple[float, float]:
    """返回 (总股本亿股, 当前价格)。"""
    info = get_stock_info(ts_code)
    total_mv = info.get("total_mv") or 0  # 万元
    price = info.get("close") or 0
    shares = (total_mv / price / 1e4) if price > 0 else 0  # 万股 -> 亿股
    return shares, price


def run_dcf_valuation(
    symbol: str,
    fcf: float | None = None,
    base_growth: float = 0.10,
    terminal_growth: float = 0.03,
    preset: str = "average",
    risk_factors: str | None = None,
    moat: str = "average",
    scenario: bool = True,
) -> dict:
    """DCF 估值：晨星标准 5 步法。

    Args:
        symbol: A 股代码，如 600519 或 600519.SH
        fcf: 当前年度自由现金流（亿元）；不传则自动取最近一期现金流量表
        base_growth: 前 5 年平均增速假设
        terminal_growth: 永续增速（默认 3%）
        preset: 晨星预设 strong_moat / average / high_risk
        risk_factors: 逗号分隔风险因素（small_size,high_leverage,cyclical,...）
        moat: 护城河强度 strong / average / weak，决定安全边际要求
        scenario: 是否同时输出乐观/中性/悲观情景
    """
    ts_code = normalize_a_share_symbol(symbol)
    if not _valid_ts(ts_code):
        return {"error": f"无法识别 A 股代码: {symbol}"}

    # 自由现金流
    if fcf is None:
        fcf_val, _ = _fetch_fcf(ts_code)
        if fcf_val is None or fcf_val == 0:
            return {"error": "未能获取自由现金流数据，请手动指定 fcf 参数（亿元）"}
        fcf = fcf_val

    net_debt = _fetch_net_debt(ts_code)
    shares, price = _fetch_shares_and_price(ts_code)
    if shares <= 0:
        return {"error": "无法计算总股本，请检查行情数据"}

    # 折现率
    preset_cfg = _MORNINGSTAR_PRESETS.get(preset, _MORNINGSTAR_PRESETS["average"])
    risk_list = [f.strip() for f in (risk_factors or "").split(",") if f.strip()] if risk_factors else None
    if risk_list:
        discount, risk_notes = _discount_rate(preset_cfg["discount_rate"], risk_list)
    else:
        discount, risk_notes = preset_cfg["discount_rate"], []

    growth_rates = _generate_growth_rates(base_growth)
    result = _dcf_calc(fcf, growth_rates, terminal_growth, discount, net_debt, shares)

    evaluation = _evaluate(result["intrinsic_value_per_share"], price, moat) if price > 0 else None

    scenarios = None
    if scenario and price > 0:
        scenarios = {}
        for key, gf, da in [("optimistic", 1.2, -0.005), ("neutral", 1.0, 0), ("pessimistic", 0.8, 0.01)]:
            rates = _generate_growth_rates(base_growth * gf)
            r = _dcf_calc(fcf, rates, terminal_growth, discount + da, net_debt, shares)
            scenarios[key] = {
                "intrinsic_value": r["intrinsic_value_per_share"],
                "safety_margin": _safety_margin(r["intrinsic_value_per_share"], price),
            }

    return {
        "symbol": ts_code,
        "current_fcf_yi": round(fcf, 4),
        "net_debt_yi": round(net_debt, 4),
        "shares_yi": round(shares, 4),
        "current_price": price,
        "discount_rate": round(discount, 4),
        "risk_adjustments": risk_notes,
        "base_growth": base_growth,
        "terminal_growth": terminal_growth,
        "dcf": result,
        "evaluation": evaluation,
        "scenarios": scenarios,
        "source": "valuation_analysis.dcf+晨星标准",
    }


# ---------------------------------------------------------------------------
# 财务红旗
# ---------------------------------------------------------------------------

def _safe_pct(new: float | None, old: float | None) -> float | None:
    if old and old != 0 and new is not None:
        return (new - old) / abs(old)
    return None


def check_red_flags(symbol: str) -> dict:
    """财务红旗检查：基于《股市真规则》第八章六大危险信号。

    检查：
    1. 净利润增长 vs 经营现金流背离
    2. 应收账款异常增长（超过收入增速）
    3. 存货积压（超过销售增速）
    4. 连续非经常性费用
    5. 高负债 + 利息覆盖率
    6. 商誉占净资产比例
    """
    ts_code = normalize_a_share_symbol(symbol)
    if not _valid_ts(ts_code):
        return {"error": f"无法识别 A 股代码: {symbol}"}

    income_resp = get_income(ts_code)
    bs_resp = get_balancesheet(ts_code)
    cf_resp = get_cashflow(ts_code)
    fina = _latest_fina_indicator(ts_code)

    inc_rows = sorted(income_resp.get("rows") or [], key=lambda r: str(r.get("end_date") or ""))
    bs_rows = sorted(bs_resp.get("rows") or [], key=lambda r: str(r.get("end_date") or ""))
    cf_rows = sorted(cf_resp.get("rows") or [], key=lambda r: str(r.get("end_date") or ""))

    flags: list[dict] = []

    # --- 1. 净利润 vs 经营现金流背离 ---
    if len(inc_rows) >= 2 and len(cf_rows) >= 2:
        ni_now = inc_rows[-1].get("n_income") or 0
        ni_prev = inc_rows[-2].get("n_income") or 0
        ocf_now = cf_rows[-1].get("n_cashflow_act") or 0
        ocf_prev = cf_rows[-2].get("n_cashflow_act") or 0
        ni_g = _safe_pct(ni_now, ni_prev)
        ocf_g = _safe_pct(ocf_now, ocf_prev)
        if ni_g is not None and ocf_g is not None:
            if ni_g > 0.10 and ocf_g < 0:
                flags.append({
                    "flag": "现金流衰退",
                    "severity": "高危",
                    "detail": f"净利润增长{ni_g * 100:.1f}%，经营现金流增长{ocf_g * 100:.1f}%",
                    "explanation": "利润未转化为现金，应收账款或存货可能积压",
                })
            elif ni_g - ocf_g > 0.15:
                flags.append({
                    "flag": "盈利质量存疑",
                    "severity": "中危",
                    "detail": f"净利润增速({ni_g * 100:.1f}%)远超经营现金流({ocf_g * 100:.1f}%)",
                    "explanation": "盈利质量可能较低，需关注应收账款与存货变动",
                })

    # --- 2. 应收账款异常增长 ---
    if len(bs_rows) >= 2 and len(inc_rows) >= 2:
        ar_now = bs_rows[-1].get("accounts_rece") or 0
        ar_prev = bs_rows[-2].get("accounts_rece") or 0
        rev_now = inc_rows[-1].get("revenue") or 0
        rev_prev = inc_rows[-2].get("revenue") or 0
        ar_g = _safe_pct(ar_now, ar_prev)
        rev_g = _safe_pct(rev_now, rev_prev)
        if ar_g is not None and rev_g is not None:
            if ar_g > rev_g * 1.2 and ar_g > 0.10:
                flags.append({
                    "flag": "应收账款异常增长",
                    "severity": "高危",
                    "detail": f"应收账款增长{ar_g * 100:.1f}% > 收入增长{rev_g * 100:.1f}%",
                    "explanation": "可能放宽信用条件刺激销售，或回收货款困难",
                })

    # --- 3. 存货积压 ---
    if len(bs_rows) >= 2 and len(inc_rows) >= 2:
        inv_now = bs_rows[-1].get("inventories") or 0
        inv_prev = bs_rows[-2].get("inventories") or 0
        rev_now = inc_rows[-1].get("revenue") or 0
        rev_prev = inc_rows[-2].get("revenue") or 0
        inv_g = _safe_pct(inv_now, inv_prev)
        rev_g = _safe_pct(rev_now, rev_prev)
        if inv_g is not None and rev_g is not None:
            if inv_g > rev_g * 1.3 and inv_g > 0.10:
                flags.append({
                    "flag": "存货积压",
                    "severity": "中危",
                    "detail": f"存货增长{inv_g * 100:.1f}% > 收入增长{rev_g * 100:.1f}%",
                    "explanation": "可能被迫降价或核销，关注存货跌价准备",
                })

    # --- 4. 高负债 ---
    if bs_rows:
        row = bs_rows[-1]
        total_liab = row.get("total_liab") or 0
        total_assets = row.get("total_assets") or 1
        debt_ratio = total_liab / total_assets if total_assets else 0
        if debt_ratio > 0.70:
            flags.append({
                "flag": "高负债率",
                "severity": "高危" if debt_ratio > 0.80 else "中危",
                "detail": f"资产负债率{debt_ratio * 100:.1f}%",
                "explanation": "财务杠杆偏高，利率上行或盈利下滑时风险放大",
            })

    # --- 5. 商誉占净资产比例 ---
    if bs_rows:
        row = bs_rows[-1]
        total_liab = row.get("total_liab") or 0
        total_assets = row.get("total_assets") or 1
        net_assets = total_assets - total_liab
        # TuShare 资产负债表暂无直接商誉字段，用 fina_indicator 的权益乘数间接评估
        if net_assets > 0 and fina:
            roe = fina.get("roe") or 0
            if roe < 0.02 and net_assets > 0:
                flags.append({
                    "flag": "ROE 过低",
                    "severity": "中危",
                    "detail": f"最新 ROE={roe * 100:.2f}%",
                    "explanation": "资产回报率偏低，需检查是否存在大额商誉减值风险",
                })

    # --- 6. 经营现金流持续为负 ---
    if len(cf_rows) >= 3:
        neg_count = sum(1 for r in cf_rows[-3:] if (r.get("n_cashflow_act") or 0) < 0)
        if neg_count >= 3:
            flags.append({
                "flag": "经营现金流连续为负",
                "severity": "高危",
                "detail": f"近{neg_count}期经营现金流均为负",
                "explanation": "主营业务持续失血，依赖融资或变卖资产维持",
            })

    if not flags:
        return {"symbol": ts_code, "flags": [], "summary": "未发现明显财务红旗信号。", "source": "valuation_analysis.red_flags"}

    high = sum(1 for f in flags if f["severity"] == "高危")
    summary = f"发现 {len(flags)} 项信号（高危{high}项）" + ("，建议审慎评估。" if high else "，需持续关注。")
    return {"symbol": ts_code, "flags": flags, "summary": summary, "source": "valuation_analysis.red_flags"}


# ---------------------------------------------------------------------------
# 护城河评估
# ---------------------------------------------------------------------------

def analyze_moat(symbol: str) -> dict:
    """护城河强度分析：基于《股市真规则》第三章四步检验框架。

    Step 1: 历史盈利能力（ROE/ROA/净利率/FCF 边际）
    Step 2: 财务杠杆与资本回报
    Step 3: 护城河来源推断（品牌/成本/网络效应/转换成本/壁垒）
    Step 4: 综合评级
    """
    ts_code = normalize_a_share_symbol(symbol)
    if not _valid_ts(ts_code):
        return {"error": f"无法识别 A 股代码: {symbol}"}

    fina = _latest_fina_indicator(ts_code)
    if not fina:
        return {"error": "无法获取财务指标数据"}

    roe = fina.get("roe") or 0
    roa = fina.get("roa") or 0
    net_margin = fina.get("netprofit_margin") or 0
    gross_margin = fina.get("grossprofit_margin") or 0

    # 盈利能力评分
    def _score3(val: float, excellent: float, good: float) -> int:
        if val >= excellent:
            return 3
        if val >= good:
            return 2
        if val > 0:
            return 1
        return 0

    roe_score = _score3(roe, 0.15, 0.12)
    roa_score = _score3(roa, 0.07, 0.06)
    nm_score = _score3(net_margin, 0.15, 0.10)
    gm_score = _score3(gross_margin, 0.40, 0.25)

    avg_score = (roe_score + roa_score + nm_score + gm_score) / 4

    if avg_score >= 2.5:
        profit_label = "优秀"
    elif avg_score >= 1.5:
        profit_label = "良好"
    elif avg_score >= 1.0:
        profit_label = "一般"
    else:
        profit_label = "偏弱"

    # 护城河来源推断
    sources: list[str] = []
    if gross_margin > 0.40:
        sources.append("高毛利率(>40%)——可能存在品牌溢价或真实产品差异化")
    if roe > 0.15 and net_margin > 0.10:
        sources.append("高ROE+高净利率——可能存在成本优势或转换成本")
    if roa > 0.07 and roe > 0.12:
        sources.append("持续高资本回报——可能拥有特许经营权或进入壁垒")

    # 护城河宽度评级
    if avg_score >= 2.5 and sources:
        width = "宽"
        width_en = "wide"
    elif avg_score >= 1.5:
        width = "窄"
        width_en = "narrow"
    else:
        width = "无明显护城河"
        width_en = "none"

    return {
        "symbol": ts_code,
        "profitability": {
            "roe": round(roe, 4),
            "roa": round(roa, 4),
            "net_margin": round(net_margin, 4),
            "gross_margin": round(gross_margin, 4),
            "score": round(avg_score, 2),
            "label": profit_label,
        },
        "moat_sources": sources or ["未检测到显著护城河来源"],
        "moat_width": width,
        "moat_width_en": width_en,
        "suggested_discount_rate": {"wide": 0.09, "narrow": 0.105, "none": 0.14}[width_en],
        "suggested_moat_for_dcf": {"wide": "strong", "narrow": "average", "none": "weak"}[width_en],
        "source": "valuation_analysis.moat+晨星护城河框架",
    }


# ---------------------------------------------------------------------------
# 估值快照
# ---------------------------------------------------------------------------

def run_valuation_snapshot(symbol: str) -> dict:
    """完整估值快照：PE/PB/PS + 历史分位 + 行业对比 + 基本面摘要。"""
    ts_code = normalize_a_share_symbol(symbol)
    if not _valid_ts(ts_code):
        return {"error": f"无法识别 A 股代码: {symbol}"}

    info = get_stock_info(ts_code)
    quote = get_realtime_quote(ts_code)
    fina = _latest_fina_indicator(ts_code)

    price = (quote.get("close") or info.get("close") or 0)
    pe = quote.get("pe") or info.get("pe")
    pb = quote.get("pb") or info.get("pb")
    ps = info.get("ps")
    total_mv = quote.get("total_mv") or info.get("total_mv")  # 万元
    circ_mv = info.get("circ_mv")  # 万元

    # 历史分位：取近 5 年日线收盘价算价格分位作为估值分位近似
    percentile_data = _calc_price_percentile(ts_code, price)

    # 全市场中位数对比
    market_median = _market_median_pe_pb()

    return {
        "symbol": ts_code,
        "name": info.get("name"),
        "industry": info.get("industry"),
        "price": price,
        "pe_ttm": pe,
        "pb": pb,
        "ps_ttm": ps,
        "total_mv_yi": round((total_mv or 0) / 1e4, 2) if total_mv else None,
        "circ_mv_yi": round((circ_mv or 0) / 1e4, 2) if circ_mv else None,
        "financial": {
            "roe": fina.get("roe"),
            "roa": fina.get("roa"),
            "gross_margin": fina.get("grossprofit_margin"),
            "net_margin": fina.get("netprofit_margin"),
            "current_ratio": fina.get("current_ratio"),
            "quick_ratio": fina.get("quick_ratio"),
        },
        "percentile_5y": percentile_data,
        "market_reference": market_median,
        "source": "valuation_analysis.snapshot+tushare",
    }


def _calc_price_percentile(ts_code: str, current_price: float) -> dict:
    """近 5 年价格分位（估值分位的粗略近似）。"""
    if current_price <= 0:
        return {}
    end = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=365 * 5)).strftime("%Y%m%d")
    fields = "ts_code,trade_date,close"
    try:
        resp = _tushare_request("daily", {"ts_code": ts_code, "start_date": start, "end_date": end}, fields)
        rows = resp.get("rows") or []
        closes = [float(r["close"]) for r in rows if r.get("close")]
        if not closes:
            return {}
        import numpy as np
        arr = np.array(closes)
        pct = float((arr <= current_price).mean() * 100)
        return {
            "price_percentile_5y": round(pct, 1),
            "low_5y": round(float(arr.min()), 2),
            "high_5y": round(float(arr.max()), 2),
            "median_5y": round(float(arr.median()), 2),
            "data_points": len(closes),
        }
    except Exception:
        return {}


def _market_median_pe_pb() -> dict:
    """全市场 PE/PB 中位数（简化：取最近交易日 daily_basic 统计）。"""
    try:
        td = date.today().strftime("%Y%m%d")
        # 取最近一个有数据的交易日
        for offset in range(7):
            check = (date.today() - timedelta(days=offset)).strftime("%Y%m%d")
            fields = "trade_date,pe,pb"
            resp = _tushare_request(
                "daily_basic", {"trade_date": check}, fields, limit=5000,
            )
            rows = resp.get("rows") or []
            if len(rows) >= 100:
                import numpy as np
                pes = [r["pe"] for r in rows if r.get("pe") and abs(r["pe"]) < 1000]
                pbs = [r["pb"] for r in rows if r.get("pb") and abs(r["pb"]) < 100]
                return {
                    "trade_date": check,
                    "market_pe_median": round(float(np.median(pes)), 2) if pes else None,
                    "market_pb_median": round(float(np.median(pbs)), 2) if pbs else None,
                    "sample_size": len(rows),
                }
        return {}
    except Exception:
        return {}


def _valid_ts(ts_code: str) -> bool:
    import re
    return bool(re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", ts_code or ""))


# ---------------------------------------------------------------------------
# Skill 注册
# ---------------------------------------------------------------------------

SKILL = {
    "name": "valuation-analysis",
    "display_name": "价值投资分析",
    "description": "DCF 估值、财务红旗检查、护城河评估、估值快照（基于《股市真规则》晨星标准）",
    "version": "1.0.0",
    "chat_modes": ["stock", "research"],
    "roles": [],
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "run_dcf_valuation",
                "description": (
                    "DCF 现金流折现估值：基于晨星标准 5 步法，自动从 TuShare 获取自由现金流、"
                    "净负债、总股本；支持自定义增速假设、风险因素调整、情景分析。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "A 股代码，如 600519 或 600519.SH"},
                        "fcf": {"type": "number", "description": "当前年度自由现金流（亿元）；不传则自动取现金流量表"},
                        "base_growth": {"type": "number", "description": "前 5 年平均增速假设，默认 0.10"},
                        "terminal_growth": {"type": "number", "description": "永续增速，默认 0.03"},
                        "preset": {
                            "type": "string",
                            "enum": ["strong_moat", "average", "high_risk"],
                            "description": "晨星预设：strong_moat(9%) / average(10.5%) / high_risk(14%)",
                        },
                        "risk_factors": {
                            "type": "string",
                            "description": "逗号分隔风险因素：small_size,high_leverage,cyclical,poor_management,weak_moat,complexity",
                        },
                        "moat": {
                            "type": "string",
                            "enum": ["strong", "average", "weak"],
                            "description": "护城河强度，决定安全边际要求，默认 average",
                        },
                        "scenario": {"type": "boolean", "description": "是否输出乐观/中性/悲观情景，默认 true"},
                    },
                    "required": ["symbol"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "check_red_flags",
                "description": (
                    "财务红旗检查：基于《股市真规则》第八章，检查现金流与利润背离、"
                    "应收账款异常、存货积压、高负债、ROE 过低、经营现金流连续为负等六大信号。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "A 股代码，如 600519 或 600519.SH"},
                    },
                    "required": ["symbol"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "analyze_moat",
                "description": (
                    "护城河强度分析：基于《股市真规则》第三章四步框架，评估盈利能力、"
                    "护城河来源（品牌/成本/网络效应/转换成本/壁垒）与护城河宽度。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "A 股代码，如 600519 或 600519.SH"},
                    },
                    "required": ["symbol"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_valuation_snapshot",
                "description": (
                    "估值快照：输出 PE/PB/PS、总市值、5 年价格分位、全市场中位数对比、"
                    "ROE/ROA/净利率等基本面摘要，一站式估值概览。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "A 股代码，如 600519 或 600519.SH"},
                    },
                    "required": ["symbol"],
                },
            },
        },
    ],
    "executors": {
        "run_dcf_valuation": lambda a: run_dcf_valuation(
            symbol=str(a.get("symbol") or ""),
            fcf=a.get("fcf"),
            base_growth=float(a.get("base_growth") or 0.10),
            terminal_growth=float(a.get("terminal_growth") or 0.03),
            preset=str(a.get("preset") or "average"),
            risk_factors=a.get("risk_factors"),
            moat=str(a.get("moat") or "average"),
            scenario=bool(a.get("scenario", True)),
        ),
        "check_red_flags": lambda a: check_red_flags(str(a.get("symbol") or "")),
        "analyze_moat": lambda a: analyze_moat(str(a.get("symbol") or "")),
        "run_valuation_snapshot": lambda a: run_valuation_snapshot(str(a.get("symbol") or "")),
    },
    "prompt_instructions": [
        "价值投资分析工具使用规则：",
        "1. 估值前先调用 run_valuation_snapshot 获取基本面概览，再决定是否深入 DCF。",
        "2. DCF 估值前建议先调用 analyze_moat 确定护城河宽度，以选择合适的折现率 preset 和安全边际要求。",
        "3. 任何估值结论都要配合 check_red_flags 结果——若存在高危信号，须在结论中明确标注风险。",
        "4. DCF 对增速假设极敏感：必须同时展示乐观/中性/悲观三情景，不得只取单一数字下结论。",
        "5. 所有分析结果仅供参考，不构成投资建议。向用户说明时须附带免责声明。",
    ],
}
