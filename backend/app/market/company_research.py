from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any

from backend.app.market.position_decision import (
    build_snapshot_id,
    calculate_technical_summary,
    compact_capital_snapshot,
    compact_fundamental_snapshot,
    compact_rows,
)
from backend.app.market.stock_tools import execute_stock_tool, normalize_a_share_symbol


def _safe_stock_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        result = execute_stock_tool(name, args)
        return result if isinstance(result, dict) else {"error": f"{name} 返回格式异常"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{name} 调用失败: {exc}"}


def _normalize_symbol(symbol: str) -> str:
    ts_code = normalize_a_share_symbol(str(symbol or ""))
    return ts_code if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", ts_code) else ""


def _rows(result: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        value = result.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _row_date(row: dict[str, Any]) -> str | None:
    for key in (
        "trade_date",
        "ann_date",
        "published_at",
        "report_date",
        "end_date",
        "date",
    ):
        value = row.get(key)
        if value:
            return str(value)
    return None


def _latest_date(rows: list[dict[str, Any]]) -> str | None:
    values = [value for value in (_row_date(row) for row in rows) if value]
    return max(values, default=None)


def _market_timeline(
    bars: list[dict[str, Any]],
    limit_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in limit_rows:
        trade_date = str(row.get("trade_date") or "")
        if not trade_date:
            continue
        status = str(
            row.get("limit")
            or row.get("limit_type")
            or row.get("status")
            or row.get("up_stat")
            or ""
        )
        label = (
            "涨停/炸板记录"
            if any(term in status.upper() for term in ("U", "Z", "涨", "炸"))
            else "涨跌停记录"
        )
        key = (trade_date, label)
        if key in seen:
            continue
        seen.add(key)
        events.append(
            {
                "trade_date": trade_date,
                "type": label,
                "status": status or None,
                "close": row.get("close"),
                "pct_chg": row.get("pct_chg"),
                "first_time": row.get("first_time"),
                "last_time": row.get("last_time"),
                "open_times": row.get("open_times"),
                "source": "TuShare limit_list_d",
            }
        )
    for row in bars:
        pct_chg = row.get("pct_chg")
        try:
            move = float(pct_chg)
        except (TypeError, ValueError):
            continue
        if abs(move) < 7:
            continue
        trade_date = str(row.get("trade_date") or "")
        key = (trade_date, "大幅波动")
        if not trade_date or key in seen:
            continue
        seen.add(key)
        events.append(
            {
                "trade_date": trade_date,
                "type": "大幅波动",
                "close": row.get("close"),
                "pct_chg": move,
                "volume": row.get("vol") or row.get("volume"),
                "amount": row.get("amount"),
                "source": "TuShare daily",
            }
        )
    events.sort(key=lambda item: str(item.get("trade_date") or ""), reverse=True)
    return events[:40]


def _event_material(
    announcements: dict[str, Any],
    news: dict[str, Any],
) -> dict[str, Any]:
    announcement_rows = _rows(announcements, "rows", "results")[:30]
    news_rows = _rows(news, "results", "rows")[:12]
    return {
        "announcements": announcement_rows,
        "news": news_rows,
        "checked_sources": [
            {
                "source": "TuShare anns_d",
                "ok": "error" not in announcements,
                "error": announcements.get("error"),
            },
            {
                "source": "Tavily Search",
                "ok": "error" not in news,
                "error": news.get("error"),
            },
        ],
        "data_as_of": max(
            (
                value
                for value in (
                    _latest_date(announcement_rows),
                    _latest_date(news_rows),
                )
                if value
            ),
            default=None,
        ),
    }


def _counter_evidence(
    *,
    market: dict[str, Any],
    technical: dict[str, Any],
    financial: dict[str, Any],
    capital: dict[str, Any],
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    facts: list[dict[str, Any]] = []
    if market.get("stale"):
        facts.append(
            {
                "dimension": "数据时效",
                "fact": "最新行情被标记为陈旧，当前价格判断可能失真。",
                "severity": "high",
                "data_as_of": market.get("trade_date"),
            }
        )
    drawdown = technical.get("max_drawdown_pct")
    if isinstance(drawdown, (int, float)) and drawdown <= -20:
        facts.append(
            {
                "dimension": "价格风险",
                "fact": f"样本窗口最大回撤为 {drawdown:.2f}%。",
                "severity": "high",
                "data_as_of": technical.get("end_trade_date"),
            }
        )
    latest_financial = financial.get("fundamental", {}).get("latest_financial", {})
    for field, label in (
        ("q_profit_yoy", "单季度利润同比"),
        ("netprofit_yoy", "净利润同比"),
        ("q_sales_yoy", "单季度营收同比"),
        ("revenue_yoy", "营收同比"),
    ):
        value = latest_financial.get(field)
        if isinstance(value, (int, float)) and value < 0:
            facts.append(
                {
                    "dimension": "财务反证",
                    "fact": f"{label}为 {value:.2f}%。",
                    "severity": "high" if value <= -30 else "medium",
                    "data_as_of": financial.get("data_as_of"),
                }
            )
    if capital.get("errors"):
        facts.append(
            {
                "dimension": "资金证据缺口",
                "fact": "部分资金、两融或筹码数据源不可用，不能据此断言主力行为。",
                "severity": "medium",
                "data_as_of": capital.get("data_as_of"),
            }
        )
    if errors:
        facts.append(
            {
                "dimension": "研究缺口",
                "fact": f"{len(errors)} 个标准数据源未成功返回。",
                "severity": "medium",
                "data_as_of": market.get("trade_date"),
            }
        )
    if not facts:
        facts.append(
            {
                "dimension": "反方检查",
                "fact": "标准底稿未发现单一决定性反证，仍需结合公告原文和行业比较验证。",
                "severity": "low",
                "data_as_of": market.get("trade_date"),
            }
        )
    return {
        "facts": facts[:12],
        "data_as_of": max(
            (
                str(item.get("data_as_of"))
                for item in facts
                if item.get("data_as_of")
            ),
            default=market.get("trade_date"),
        ),
    }


def get_company_research_bundle(
    symbol: str,
    *,
    lookback_days: int = 365,
    horizon: str = "中期",
) -> dict[str, Any]:
    ts_code = _normalize_symbol(symbol)
    if not ts_code:
        return {"error": f"无法识别标的: {symbol}"}

    lookback = max(90, min(int(lookback_days or 365), 730))
    today = date.today()
    market_start = (today - timedelta(days=lookback)).strftime("%Y%m%d")
    financial_start = (today - timedelta(days=365 * 5 + 2)).strftime("%Y%m%d")
    event_start = (today - timedelta(days=min(lookback, 180))).strftime("%Y%m%d")
    capital_start = (today - timedelta(days=120)).strftime("%Y%m%d")
    end_date = today.strftime("%Y%m%d")

    calls = {
        "latest": ("get_latest_price", {"symbol": ts_code}),
        "history": ("get_daily_history", {"symbol": ts_code, "days": min(500, lookback + 80)}),
        "fundamental": ("get_stock_info", {"symbol": ts_code}),
        "income": ("get_income", {"symbol": ts_code, "start_date": financial_start}),
        "balance": ("get_balancesheet", {"symbol": ts_code, "start_date": financial_start}),
        "cashflow": ("get_cashflow", {"symbol": ts_code, "start_date": financial_start}),
        "capital_flow": ("get_capital_flow", {"symbol": ts_code, "days": 20}),
        "margin": (
            "get_margin_detail",
            {"symbol": ts_code, "start_date": capital_start, "end_date": end_date},
        ),
        "chip": ("get_chip_distribution", {"symbol": ts_code}),
        "limit_list": (
            "get_limit_list",
            {"symbol": ts_code, "start_date": market_start, "end_date": end_date},
        ),
        "announcements": (
            "get_anns",
            {"symbol": ts_code, "start_date": event_start, "end_date": end_date},
        ),
        "news": (
            "search_stock_news",
            {
                "query": f"{ts_code} 公司公告 业绩 订单 风险 催化",
                "symbol": ts_code,
                "max_results": 8,
            },
        ),
        "top_list": ("get_top_list", {"symbol": ts_code}),
        "research_consensus": (
            "get_report_rc",
            {"symbol": ts_code, "start_date": financial_start, "end_date": end_date},
        ),
    }
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="company-research") as pool:
        futures = {
            key: pool.submit(_safe_stock_tool, tool_name, args)
            for key, (tool_name, args) in calls.items()
        }
        datasets = {key: future.result() for key, future in futures.items()}

    latest = datasets["latest"]
    history = datasets["history"]
    bars = _rows(history, "bars", "rows")
    latest_bar = max(
        bars,
        key=lambda row: str(row.get("trade_date") or ""),
        default={},
    )
    latest_price = latest.get("price")
    if latest_price is None:
        latest_price = latest_bar.get("close")
    identity_source = datasets["fundamental"]
    fundamental = compact_fundamental_snapshot(identity_source)
    identity = {
        "symbol": ts_code,
        "name": latest.get("name") or identity_source.get("name") or history.get("name"),
        "industry": identity_source.get("industry"),
        "market": identity_source.get("market"),
        "list_date": identity_source.get("list_date"),
    }

    technical = calculate_technical_summary(bars)
    limit_rows = _rows(datasets["limit_list"], "rows", "results")
    market = {
        "price": latest_price,
        "pre_close": latest.get("pre_close") or latest_bar.get("pre_close"),
        "pct_chg": latest.get("pct_chg") or latest_bar.get("pct_chg"),
        "trade_date": latest.get("trade_date") or latest_bar.get("trade_date"),
        "expected_trade_date": latest.get("expected_trade_date"),
        "market_open": latest.get("market_open"),
        "stale": bool(latest.get("stale")),
        "intraday_unavailable": bool(latest.get("intraday_unavailable")),
        "freshness_note": latest.get("freshness_note"),
        "timeline": _market_timeline(bars, limit_rows),
        "limit_records": limit_rows[:30],
        "source": latest.get("source") or history.get("source") or "TuShare",
    }

    income_trend = compact_rows(
        datasets["income"].get("rows"),
        fields=(
            "ann_date",
            "end_date",
            "revenue",
            "n_income_parent",
            "net_profit_excl_min_int_inc",
            "ebit",
            "ebitda",
        ),
        limit=8,
    )
    balance_trend = compact_rows(
        datasets["balance"].get("rows"),
        fields=(
            "ann_date",
            "end_date",
            "total_assets",
            "total_liab",
            "money_cap",
            "accounts_rece",
            "inventories",
        ),
        limit=8,
    )
    cashflow_trend = compact_rows(
        datasets["cashflow"].get("rows"),
        fields=(
            "ann_date",
            "end_date",
            "n_cashflow_act",
            "n_cashflow_inv",
            "n_cash_flows_finance",
            "free_cashflow",
        ),
        limit=8,
    )
    financial_as_of = max(
        (
            value
            for value in (
                fundamental.get("data_as_of"),
                _latest_date(income_trend),
                _latest_date(balance_trend),
                _latest_date(cashflow_trend),
            )
            if value
        ),
        default=None,
    )
    valuation = {
        **(fundamental.get("valuation") or {}),
        "data_as_of": (fundamental.get("valuation") or {}).get("trade_date")
        or market.get("trade_date"),
    }
    financial = {
        "fundamental": fundamental,
        "income_trend": income_trend,
        "balance_trend": balance_trend,
        "cashflow_trend": cashflow_trend,
        "data_as_of": financial_as_of,
    }
    capital = compact_capital_snapshot(
        flow=datasets["capital_flow"],
        margin=datasets["margin"],
        chip=datasets["chip"],
    )
    events = _event_material(datasets["announcements"], datasets["news"])

    try:
        from backend.app.market.digital_human_tools import (
            get_recent_internal_evidence,
            get_symbol_documents,
            get_symbol_reports,
        )

        internal = {
            "evidence": get_recent_internal_evidence(symbol=ts_code, limit=10),
            "reports": get_symbol_reports(ts_code, limit=8),
            "documents": get_symbol_documents(ts_code, limit=8),
        }
    except Exception as exc:  # noqa: BLE001
        internal = {"error": f"内部研究材料读取失败: {exc}"}

    errors = [
        {"dataset": key, "error": str(result.get("error"))}
        for key, result in datasets.items()
        if result.get("error")
    ]
    counter_evidence = _counter_evidence(
        market=market,
        technical=technical,
        financial=financial,
        capital=capital,
        errors=errors,
    )
    data_availability = {
        "market": {
            "available": bool(market.get("price") is not None and market.get("trade_date")),
            "as_of": market.get("trade_date"),
        },
        "financial": {
            "available": bool(income_trend and (balance_trend or cashflow_trend)),
            "as_of": financial.get("data_as_of"),
        },
        "valuation": {
            "available": any(
                valuation.get(key) is not None for key in ("pe", "pb", "ps", "total_mv")
            ),
            "as_of": valuation.get("data_as_of"),
        },
        "events": {
            "available": any(
                bool(item.get("ok"))
                for item in events.get("checked_sources") or []
                if isinstance(item, dict)
            ),
            "as_of": events.get("data_as_of") or market.get("trade_date"),
        },
        "technical": {
            "available": int(technical.get("bar_count") or 0) >= 60,
            "as_of": technical.get("end_trade_date"),
        },
        "capital": {
            "available": bool(
                capital.get("flow_recent")
                or capital.get("margin_recent")
                or capital.get("chip")
            ),
            "as_of": capital.get("data_as_of"),
        },
        "counter_view": {
            "available": bool(counter_evidence.get("facts")),
            "as_of": counter_evidence.get("data_as_of"),
        },
    }
    snapshot_payload = {
        "identity": identity,
        "market": {key: value for key, value in market.items() if key != "timeline"},
        "financial_as_of": financial.get("data_as_of"),
        "capital_as_of": capital.get("data_as_of"),
        "event_as_of": events.get("data_as_of"),
        "data_availability": data_availability,
    }
    return {
        "source": "hermes.company_research_bundle",
        "source_type": "company_research_bundle",
        "data_as_of": market.get("trade_date") or technical.get("end_trade_date"),
        "symbol": ts_code,
        "identity": identity,
        "market": market,
        "technical": technical,
        "financial": financial,
        "valuation": valuation,
        "events": events,
        "capital": capital,
        "counter_evidence": counter_evidence,
        "internal_evidence": internal,
        "supplementary": {
            "top_list": datasets["top_list"],
            "research_consensus": datasets["research_consensus"],
        },
        "data_availability": data_availability,
        "errors": errors,
        "snapshot_id": build_snapshot_id(snapshot_payload),
        "context_budget": {
            "raw_history_bars": len(bars),
            "returned_recent_bars": len(technical.get("recent_bars") or []),
            "timeline_events": len(market.get("timeline") or []),
            "announcements": len(events.get("announcements") or []),
            "news": len(events.get("news") or []),
            "income_periods": len(income_trend),
            "balance_periods": len(balance_trend),
            "cashflow_periods": len(cashflow_trend),
        },
    }
