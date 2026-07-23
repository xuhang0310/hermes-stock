from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Generator


_DATA_OWNER_USER_ID: ContextVar[int | None] = ContextVar("data_owner_user_id", default=None)


def current_data_owner_user_id() -> int | None:
    return _DATA_OWNER_USER_ID.get()


@contextmanager
def data_user_scope(user_id: int | None) -> Generator[None, None, None]:
    token = _DATA_OWNER_USER_ID.set(int(user_id) if user_id is not None else None)
    try:
        yield
    finally:
        _DATA_OWNER_USER_ID.reset(token)
