"""Atomic, user-scoped idempotency for external MCP mutations."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.models import User


def run_idempotent_mutation(
    db: Session,
    user_id: int,
    tool_name: str,
    idempotency_key: str,
    payload: dict[str, Any],
    operation: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Commit an operation and its receipt together, or roll both back.

    The caller supplies a dedicated session and an operation which does not commit.
    A locking User read serializes this user's MCP mutations across workers; the
    receipt is also a locking read so MySQL repeatable-read cannot reuse an older
    snapshot. The unique receipt constraint remains the final duplicate guard.
    """
    from backend.app.core.models import McpMutationReceipt

    try:
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("必须提供认证用户")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 80:
            raise ValueError("idempotency_key 必须是 1 至 80 字符的非空请求编号")
        if idempotency_key != idempotency_key.strip():
            raise ValueError("idempotency_key 首尾不能包含空格")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        request_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        user = db.scalar(select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True))
        if user is None or not user.is_enabled:
            raise ValueError("用户不存在或已停用")
        receipt = db.scalar(
            select(McpMutationReceipt).where(
                McpMutationReceipt.user_id == user_id,
                McpMutationReceipt.tool_name == tool_name,
                McpMutationReceipt.idempotency_key == idempotency_key,
            ).with_for_update()
        )
        if receipt is not None:
            if receipt.request_hash != request_hash:
                raise ValueError("同一 idempotency_key 已用于不同参数，请为新操作使用新的请求编号")
            result = json.loads(receipt.result_json)
            db.commit()
            return result
        result = operation()
        if not isinstance(result, dict):
            raise ValueError("写入操作必须返回结构化结果")
        if result.get("error"):
            raise ValueError(str(result["error"]))
        result_json = json.dumps(result, ensure_ascii=False, allow_nan=False)
        db.add(McpMutationReceipt(
            user_id=user_id,
            tool_name=tool_name,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            result_json=result_json,
        ))
        db.commit()
        return json.loads(result_json)
    except BaseException:
        db.rollback()
        raise
