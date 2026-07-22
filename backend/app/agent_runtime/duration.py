from __future__ import annotations

from datetime import datetime


def elapsed_wall_clock_ms(
    started_at: datetime | str | None,
    finished_at: datetime | str | None,
) -> int | None:
    """Return end-to-end wall-clock time for a durable run."""

    start = _as_datetime(started_at)
    finish = _as_datetime(finished_at)
    if start is None or finish is None:
        return None
    if start.tzinfo is not None and finish.tzinfo is None:
        finish = finish.replace(tzinfo=start.tzinfo)
    elif finish.tzinfo is not None and start.tzinfo is None:
        start = start.replace(tzinfo=finish.tzinfo)
    return max(0, int((finish - start).total_seconds() * 1000))


def _as_datetime(value: datetime | str | None) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
