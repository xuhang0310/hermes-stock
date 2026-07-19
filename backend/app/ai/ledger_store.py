"""L3 事实记忆 — 不可篡改台账 + LLM 调用留档（append-only JSONL + 哈希链）。

设计（借鉴 claw-code rust/crates/runtime/src/session.rs 的追加写/原子写/轮转）：
  - append-only：只追加，不修改不删除 → 天然不可 UPDATE/DELETE（不靠 DB 权限层）
  - 哈希链：每条 record 含 prev_hash + hash，hash = sha256(prev_hash|type|ts|canonical(data))
    改任何一条 data，后续所有 hash 对不上 → verify() 立即检出
  - 跨文件链：轮转后新文件首条 prev_hash = 旧文件末 hash，链不断
  - 原子追加：O_APPEND + flush + fsync，崩溃安全
  - 线程锁：单进程并发安全（多进程需加文件锁，生产再固）

两类 ledger（各自独立哈希链）：
  - ledger：预测台账（instruction/execution/settlement），永久，data/ledger/
  - llm_calls：LLM 调用留档（方案 7.2 全量留档），data/llm_calls/

MySQL 索引表（可查询）后期加，可从 JSONL rebuild（接口已留）。
台账原件永远不接 UPDATE/DELETE 接口；任何"修正"走 append 一条 correction record。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.app.core.applog import log_event

_REPO_ROOT = Path(__file__).resolve().parents[3]  # backend/app/ai/ledger_store.py -> repo root
# 数据目录可配（生产放 shared/ 防止 release 整体替换时丢失；本地默认项目 data/）
_LEDGER_ROOT = Path(os.getenv("STOCK_LEDGER_DIR") or str(_REPO_ROOT / "data" / "ledger"))
_CALLS_ROOT = Path(os.getenv("STOCK_CALLS_DIR") or str(_REPO_ROOT / "data" / "llm_calls"))

ROTATE_BYTES = 64 * 1024 * 1024  # 64MB 轮转
ZERO_HASH = "0" * 64


# ---------------------------------------------------------------------------
# 哈希链
# ---------------------------------------------------------------------------


def _canonical_json(data: Any) -> str:
    """稳定序列化：sort_keys + 紧凑分隔，保证同 data 永远同 hash。"""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record_hash(prev_hash: str, rtype: str, ts: str, data: Any) -> str:
    payload = f"{prev_hash}|{rtype}|{ts}|{_canonical_json(data)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# AppendOnlyLedger — 单 ledger 文件 + 哈希链 + 轮转
# ---------------------------------------------------------------------------


class AppendOnlyLedger:
    """单 ledger（append-only JSONL + 哈希链 + 轮转）。线程安全（单进程）。"""

    def __init__(self, root: Path, name: str, *, rotate_bytes: int = ROTATE_BYTES):
        self._root = root
        self._name = name
        self._rotate = rotate_bytes
        self._current = root / f"{name}.jsonl"
        self._lock = threading.Lock()
        root.mkdir(parents=True, exist_ok=True)
        self._last_hash, self._last_seq = self._read_tail()

    def _read_tail(self) -> tuple[str, int]:
        """启动读当前文件末条 (hash, seq)；空文件返回 (ZERO_HASH, 0)。"""
        if not self._current.exists():
            return ZERO_HASH, 0
        last_hash, last_seq = ZERO_HASH, 0
        try:
            with self._current.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        last_hash = rec.get("hash", ZERO_HASH)
                        last_seq = int(rec.get("seq", 0))
                    except (json.JSONDecodeError, ValueError, TypeError):
                        continue
        except OSError:
            pass
        return last_hash, last_seq

    def append(self, rtype: str, data: dict) -> dict:
        """追加一条 record，返回完整 record。线程安全；任何异常不抛（留档不影响主流程）。"""
        ts = datetime.now(timezone.utc).isoformat()
        try:
            with self._lock:
                self._maybe_rotate()
                seq = self._last_seq + 1
                h = _record_hash(self._last_hash, rtype, ts, data)
                record = {
                    "seq": seq, "type": rtype,
                    "prev_hash": self._last_hash, "hash": h,
                    "ts": ts, "data": data,
                }
                line = json.dumps(record, ensure_ascii=False) + "\n"
                with self._current.open("a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())  # 崩溃安全
                self._last_hash = h
                self._last_seq = seq
                return record
        except Exception as exc:  # noqa: BLE001  留档绝不影响主流程
            log_event("agent", "ERROR", f"L3 台账追加失败（不影响主流程）：{str(exc)[:160]}", to_db=False)
            return {}

    def _maybe_rotate(self) -> None:
        if not self._current.exists():
            return
        try:
            if self._current.stat().st_size < self._rotate:
                return
        except OSError:
            return
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        rotated = self._root / f"{self._name}.{ts}.jsonl"
        try:
            self._current.rename(rotated)
        except OSError:
            return
        # last_hash 保持（跨文件链不断）；新文件 seq 从 1
        self._last_seq = 0

    def verify(self) -> tuple[bool, list[str]]:
        """校验哈希链完整性。返回 (ok, 错误列表)。轮转文件（旧）在前，当前文件在后。"""
        errors: list[str] = []
        prev = ZERO_HASH
        rotated = sorted(self._root.glob(f"{self._name}.*.jsonl"))
        files = rotated + ([self._current] if self._current.exists() else [])
        for fpath in files:
            try:
                with fpath.open("r", encoding="utf-8") as f:
                    for lineno, line in enumerate(f, 1):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            errors.append(f"{fpath.name}:{lineno} JSON 解析失败")
                            prev = ZERO_HASH
                            continue
                        expected = _record_hash(
                            prev, rec.get("type", ""), rec.get("ts", ""), rec.get("data")
                        )
                        if rec.get("prev_hash") != prev:
                            errors.append(
                                f"{fpath.name}:{lineno} prev_hash 链断（期望 {prev[:8]}..，"
                                f"实际 {str(rec.get('prev_hash'))[:8]}..）"
                            )
                        if rec.get("hash") != expected:
                            errors.append(f"{fpath.name}:{lineno} hash 不匹配（data 疑似被篡改）")
                        prev = rec.get("hash", ZERO_HASH)
            except OSError as exc:
                errors.append(f"{fpath.name} 读取失败：{exc}")
        return (len(errors) == 0, errors)


# ---------------------------------------------------------------------------
# 单例：台账 + 调用留档
# ---------------------------------------------------------------------------

_ledger: AppendOnlyLedger | None = None
_calls: AppendOnlyLedger | None = None


def _get_ledger() -> AppendOnlyLedger:
    global _ledger
    if _ledger is None:
        _ledger = AppendOnlyLedger(_LEDGER_ROOT, "ledger")
    return _ledger


def _get_calls() -> AppendOnlyLedger:
    global _calls
    if _calls is None:
        _calls = AppendOnlyLedger(_CALLS_ROOT, "calls")
    return _calls


# ---------------------------------------------------------------------------
# 业务 API
# ---------------------------------------------------------------------------


def append_instruction(data: dict) -> dict:
    """台账：预测指令（买/卖/替换/持有）。P2 首席决策官下达指令时调用。

    data 建议字段：action, symbol, ref_price, target_price, stop_price,
    position_size, confidence, thesis, review_id, market_snapshot...
    任何人（含 AI）不得事后修改；修正走 append 一条 correction record。
    """
    return _get_ledger().append("instruction", data)


def append_execution(data: dict) -> dict:
    """台账：用户实际成交回报（关联表，唯一可人工写入的关联数据）。

    data 建议字段：instruction_seq（关联的指令）, user_filled, fill_price, position_size...
    用于 6.3 执行偏差归因与 7.1 真实账户回撤警报。
    """
    return _get_ledger().append("execution", data)


def append_settlement(data: dict) -> dict:
    """台账：到期结算（超额收益口径）。

    data 建议字段：instruction_seq, excess_return, benchmark（沪深300/中证500/800）,
    max_floating_pnl, thesis_fulfillment...
    """
    return _get_ledger().append("settlement", data)


def append_correction(data: dict) -> dict:
    """台账：修正记录（不就地改原条，追加一条 correction，关联原 seq）。"""
    return _get_ledger().append("correction", data)


def append_llm_call(
    *,
    model: str,
    messages: list[dict],
    response: dict | None,
    usage: dict | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
) -> dict:
    """LLM 调用留档（方案 7.2 全量留档）。fire-and-forget，不抛错。

    存 prompt(messages) + response + 真实模型版本 + usage(token)。成本后期按 usage 算。
    注：留档的是实际发给 LLM 的 messages（已过持仓隐私路由脱敏后），不含原始持仓明细。
    """
    data = {
        "model": model,
        "prompt": messages,
        "response": response,
        "usage": usage,
        "error": error,
        "duration_ms": duration_ms,
    }
    return _get_calls().append("llm_call", data)


def verify_ledger() -> tuple[bool, list[str]]:
    """校验台账哈希链完整性。"""
    return _get_ledger().verify()


def verify_calls() -> tuple[bool, list[str]]:
    """校验调用留档哈希链完整性。"""
    return _get_calls().verify()
