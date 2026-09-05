from __future__ import annotations

from datetime import datetime, timedelta


def history_dates(target_trade_date: str = "20260828", count: int = 21) -> list[str]:
    day = datetime.strptime(target_trade_date, "%Y%m%d").date()
    dates = []
    while len(dates) < count:
        if day.weekday() < 5:
            dates.append(day.strftime("%Y%m%d"))
        day -= timedelta(days=1)
    return list(reversed(dates))
