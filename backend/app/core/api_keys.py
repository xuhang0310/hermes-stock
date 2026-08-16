from __future__ import annotations

import re
from collections.abc import Iterable


_API_KEY_SEPARATOR = re.compile(r"[\s,;]+")
TAVILY_POOL_PROVIDER = "tavily_pool"


def split_api_keys(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """Parse one or more API keys without logging or otherwise exposing them."""
    if value is None:
        return ()
    raw_values = (value,) if isinstance(value, str) else value
    keys: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        for key in _API_KEY_SEPARATOR.split(str(raw or "").strip()):
            normalized = key.strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                keys.append(normalized)
    return tuple(keys)


def merge_api_keys(*values: str | Iterable[str] | None) -> tuple[str, ...]:
    """Merge key sources in priority order and remove duplicates."""
    merged: list[str] = []
    seen: set[str] = set()
    for value in values:
        for key in split_api_keys(value):
            if key not in seen:
                seen.add(key)
                merged.append(key)
    return tuple(merged)


def serialize_api_keys(value: str | Iterable[str] | None) -> str:
    """Store a key pool in the existing database field, one key per line."""
    return "\n".join(split_api_keys(value))
