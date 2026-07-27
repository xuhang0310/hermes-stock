from __future__ import annotations

import hashlib
import json
from typing import Any


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _rounded(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def compact_rows(
    rows: Any,
    *,
    fields: tuple[str, ...],
    limit: int = 5,
    date_field: str = "end_date",
) -> list[dict[str, Any]]:
    """Keep the latest unique periods and only decision-useful fields."""

    if not isinstance(rows, list):
        return []
    normalized = [row for row in rows if isinstance(row, dict)]
    normalized.sort(
        key=lambda row: (
            str(row.get(date_field) or ""),
            str(row.get("ann_date") or row.get("trade_date") or ""),
        ),
        reverse=True,
    )
    compacted: list[dict[str, Any]] = []
    seen_periods: set[str] = set()
    for row in normalized:
        period = str(row.get(date_field) or row.get("trade_date") or "")
        if period and period in seen_periods:
            continue
        if period:
            seen_periods.add(period)
        item = {
            field: row.get(field)
            for field in fields
            if row.get(field) is not None
        }
        if item:
            compacted.append(item)
        if len(compacted) >= max(1, limit):
            break
    return compacted


def compact_fundamental_snapshot(result: dict[str, Any]) -> dict[str, Any]:
    financial = result.get("financial") if isinstance(result.get("financial"), dict) else {}
    financial_fields = (
        "end_date",
        "ann_date",
        "eps",
        "dt_eps",
        "bps",
        "ocfps",
        "grossprofit_margin",
        "netprofit_margin",
        "roe",
        "debt_to_assets",
        "current_ratio",
        "quick_ratio",
        "q_sales_yoy",
        "q_profit_yoy",
        "revenue_yoy",
        "netprofit_yoy",
    )
    return {
        "source": result.get("source"),
        "data_as_of": financial.get("end_date") or result.get("trade_date"),
        "name": result.get("name"),
        "industry": result.get("industry"),
        "market": result.get("market"),
        "list_date": result.get("list_date"),
        "valuation": {
            key: result.get(key)
            for key in ("trade_date", "close", "pe", "pb", "ps", "total_mv", "circ_mv")
            if result.get(key) is not None
        },
        "latest_financial": {
            key: financial.get(key)
            for key in financial_fields
            if financial.get(key) is not None
        },
    }


def compact_capital_snapshot(
    *,
    flow: dict[str, Any],
    margin: dict[str, Any],
    chip: dict[str, Any],
) -> dict[str, Any]:
    flow_rows = compact_rows(
        flow.get("rows"),
        fields=("trade_date", "main_net_amount", "net_mf_amount"),
        limit=10,
        date_field="trade_date",
    )
    margin_rows = compact_rows(
        margin.get("rows"),
        fields=("trade_date", "rzye", "rqye", "rzmre", "rqmcl", "rzrqye"),
        limit=10,
        date_field="trade_date",
    )
    data_as_of = max(
        (
            str(value)
            for value in (
                flow.get("trade_date"),
                margin_rows[0].get("trade_date") if margin_rows else None,
                chip.get("trade_date"),
            )
            if value
        ),
        default="",
    )
    return {
        "data_as_of": data_as_of or None,
        "flow": {
            key: flow.get(key)
            for key in (
                "source",
                "trade_date",
                "main_net_amount",
                "main_net_amount_5d",
                "main_net_amount_10d",
                "net_mf_amount",
                "net_mf_amount_5d",
                "net_mf_amount_10d",
            )
            if flow.get(key) is not None
        },
        "flow_recent": flow_rows,
        "margin_source": margin.get("source"),
        "margin_recent": margin_rows,
        "chip": {
            key: chip.get(key)
            for key in (
                "source",
                "trade_date",
                "cost_15pct",
                "cost_50pct",
                "cost_85pct",
                "avg_cost",
                "winner_rate",
            )
            if chip.get(key) is not None
        },
        "errors": [
            {"dataset": name, "error": str(result.get("error"))}
            for name, result in (("flow", flow), ("margin", margin), ("chip", chip))
            if result.get("error")
        ],
    }


def calculate_position_metrics(
    *,
    quantity: Any,
    cost_price: Any,
    latest_price: Any,
) -> dict[str, float | None]:
    qty = _number(quantity)
    cost = _number(cost_price)
    price = _number(latest_price)
    cost_amount = qty * cost if qty is not None and cost is not None else None
    market_value = qty * price if qty is not None and price is not None else None
    floating_pnl = (
        market_value - cost_amount
        if market_value is not None and cost_amount is not None
        else None
    )
    floating_pnl_pct = (
        floating_pnl / cost_amount * 100
        if floating_pnl is not None and cost_amount not in (None, 0)
        else None
    )
    break_even_distance_pct = (
        (cost / price - 1) * 100
        if cost not in (None, 0) and price not in (None, 0)
        else None
    )
    return {
        "quantity": _rounded(qty),
        "cost_price": _rounded(cost),
        "latest_price": _rounded(price),
        "cost_amount": _rounded(cost_amount, 2),
        "market_value": _rounded(market_value, 2),
        "floating_pnl": _rounded(floating_pnl, 2),
        "floating_pnl_pct": _rounded(floating_pnl_pct, 2),
        "break_even_distance_pct": _rounded(break_even_distance_pct, 2),
    }


def calculate_technical_summary(bars: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(
        (row for row in bars if isinstance(row, dict) and row.get("trade_date")),
        key=lambda row: str(row.get("trade_date")),
    )
    closes = [_number(row.get("close")) for row in ordered]
    valid_closes = [value for value in closes if value is not None]
    recent_20 = ordered[-20:]
    recent_lows = [
        value
        for value in (_number(row.get("low")) for row in recent_20)
        if value is not None
    ]
    recent_highs = [
        value
        for value in (_number(row.get("high")) for row in recent_20)
        if value is not None
    ]

    peak: float | None = None
    max_drawdown = 0.0
    for close in valid_closes:
        peak = close if peak is None else max(peak, close)
        if peak:
            max_drawdown = min(max_drawdown, close / peak - 1)

    def moving_average(window: int) -> float | None:
        if len(valid_closes) < window:
            return None
        return _mean(valid_closes[-window:])

    def period_return(days: int) -> float | None:
        if len(valid_closes) <= days or not valid_closes[-days - 1]:
            return None
        return (valid_closes[-1] / valid_closes[-days - 1] - 1) * 100

    return {
        "bar_count": len(ordered),
        "start_trade_date": ordered[0].get("trade_date") if ordered else None,
        "end_trade_date": ordered[-1].get("trade_date") if ordered else None,
        "ma5": _rounded(moving_average(5)),
        "ma10": _rounded(moving_average(10)),
        "ma20": _rounded(moving_average(20)),
        "ma60": _rounded(moving_average(60)),
        "return_5d_pct": _rounded(period_return(5), 2),
        "return_20d_pct": _rounded(period_return(20), 2),
        "support_20d": _rounded(min(recent_lows) if recent_lows else None),
        "resistance_20d": _rounded(max(recent_highs) if recent_highs else None),
        "max_drawdown_pct": _rounded(max_drawdown * 100, 2),
        "recent_bars": ordered[-12:],
    }


def build_snapshot_id(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
