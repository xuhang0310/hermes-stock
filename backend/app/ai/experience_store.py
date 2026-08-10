"""L2 经验记忆 — 按用户隔离的复盘经验库。

与 UserMemory(ai/memory.py) 严格分库：
  - UserMemory 是用户画像（按 user_id 隔离，个性化对话用）
  - 本库是用户自己的统计经验（按 user_id/tags/market_env/status 过滤）

复用 memory.py 的 _embed（嵌入）和 _rerank_scores（精排），但用独立 ChromaDB collection
("experience_entries")，与 user_memories 隔离，零改动现有用户记忆。

核心契约（对齐方案 6.4 经验库进化）：
  - 经验带 sample_size + statistical_strength，防小样本过拟合
  - 检索：标签 + 市场环境 + 向量语义 + rerank 混合，相关性门槛，单次注入上限 3 条
  - 注入留痕(ExperienceInjection)，成绩回填 post_return（依赖 L3 台账，MVP 留接口暂不回填）
  - 新条目观察模式(observing)，期满激活；长期无效自动 archived
  - market_env 标签防跨环境错配（牛市经验不注入熊市）

开发环境默认使用 ChromaDB；生产环境可通过 STOCK_VECTOR_BACKEND 切换到 Milvus。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.ai import memory as _memory
from backend.app.ai.memory import _embed, _rerank_scores, _resolve_rerank_cfg
from backend.app.core.applog import log_event
from backend.app.core.models import ExperienceEntry, ExperienceInjection
from backend.app.core.settings import settings

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

EXPERIENCE_INJECT_LIMIT = 3          # 单次注入上限（方案 6.4）
EXPERIENCE_RERANK_POOL = 24          # rerank 候选池
EXPERIENCE_TOKEN_BUDGET = 800         # 注入文本字符预算
OBSERVE_DAYS = 30                    # 新条目观察期（天），期满可激活
ARCHIVE_INJECT_THRESHOLD = 5         # 注入次数达到此值且平均回报为负则降权归档
ARCHIVE_NEG_RETURN = -0.02          # 平均注入回报低于此视为无效

# 独立向量库路径（与 user_memories 的 chroma_data 隔离；生产放 shared 防止 release 替换丢失）
_EXP_CHROMA_PATH = (
    Path(settings.experience_chroma_dir).expanduser()
    if settings.experience_chroma_dir
    else Path(__file__).resolve().parents[2] / "chroma_exp_data"
)
# 使用新集合隔离旧版无 user_id 的共享向量；旧集合保留但不再召回。
_EXP_COLLECTION = f"{settings.experience_milvus_collection}_user_v2"

_exp_col: Any = None
_exp_ready: bool | None = None
_exp_failed_at = 0.0
_STORE_RETRY_SECONDS = 60


# ---------------------------------------------------------------------------
# 向量库后端
# ---------------------------------------------------------------------------


class _ChromaExperienceStore:
    def __init__(self) -> None:
        import chromadb

        _EXP_CHROMA_PATH.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=str(_EXP_CHROMA_PATH))
        self._col = client.get_or_create_collection(
            name=_EXP_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )

    def upsert(self, ids, embeddings, metadatas, documents) -> None:
        self._col.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas, documents=documents)

    def delete_ids(self, ids: list[str]) -> None:
        self._col.delete(ids=ids)

    def delete_status(self, status: str) -> None:
        self._col.delete(where={"status": status})

    def query(
        self,
        embeddings: list[list[float]],
        n: int,
        status: str,
        user_id: int,
    ) -> list[tuple[int, float]]:
        res = self._col.query(
            query_embeddings=embeddings,
            n_results=n,
            where={"$and": [{"status": status}, {"user_id": int(user_id)}]},
            include=["distances"],
        )
        ids = res.get("ids", [[]])[0]
        dists = res.get("distances", [[]])[0]
        out: list[tuple[int, float]] = []
        for entry_id, distance in zip(ids, dists):
            try:
                out.append((int(entry_id), max(0.0, 1.0 - float(distance) / 2.0)))
            except (TypeError, ValueError):
                continue
        return out


class _MilvusExperienceStore:
    def __init__(self) -> None:
        from pymilvus import DataType, MilvusClient

        if _memory._ensure_embedding() is None or not _memory._embedding_dim:
            raise RuntimeError("嵌入维度未知，无法创建经验集合")
        self._name = _EXP_COLLECTION
        self._c = MilvusClient(uri=settings.milvus_uri, token=settings.milvus_token or "")
        if not self._c.has_collection(self._name):
            schema = self._c.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field("id", DataType.INT64, is_primary=True)
            schema.add_field("vector", DataType.FLOAT_VECTOR, dim=_memory._embedding_dim)
            schema.add_field("user_id", DataType.INT64)
            schema.add_field("status", DataType.VARCHAR, max_length=24)
            schema.add_field("tags", DataType.VARCHAR, max_length=4096)
            schema.add_field("market_env", DataType.VARCHAR, max_length=4096)
            index_params = self._c.prepare_index_params()
            index_params.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
            self._c.create_collection(
                collection_name=self._name,
                schema=schema,
                index_params=index_params,
                consistency_level="Strong",
            )
        self._c.load_collection(self._name)

    def upsert(self, ids, embeddings, metadatas, documents) -> None:
        data = [
            {
                "id": int(entry_id),
                "vector": vector,
                "user_id": int((meta or {}).get("user_id", 0)),
                "status": str((meta or {}).get("status") or ""),
                "tags": str((meta or {}).get("tags") or "")[:4096],
                "market_env": str((meta or {}).get("market_env") or "")[:4096],
            }
            for entry_id, vector, meta in zip(ids, embeddings, metadatas)
        ]
        self._c.upsert(collection_name=self._name, data=data)

    def delete_ids(self, ids: list[str]) -> None:
        self._c.delete(collection_name=self._name, ids=[int(entry_id) for entry_id in ids])

    def delete_status(self, status: str) -> None:
        if status not in {"active", "observing", "archived"}:
            raise ValueError("不支持的经验状态")
        self._c.delete(collection_name=self._name, filter=f'status == "{status}"')

    def query(
        self,
        embeddings: list[list[float]],
        n: int,
        status: str,
        user_id: int,
    ) -> list[tuple[int, float]]:
        if status not in {"active", "observing", "archived"}:
            return []
        res = self._c.search(
            collection_name=self._name,
            data=embeddings,
            limit=n,
            filter=f'status == "{status}" and user_id == {int(user_id)}',
            output_fields=[],
            consistency_level="Strong",
        )
        out: list[tuple[int, float]] = []
        for hit in (res[0] if res else []):
            try:
                cosine = float(hit.get("distance", 0.0))
                out.append((int(hit["id"]), max(0.0, min(1.0, (cosine + 1.0) / 2.0))))
            except (KeyError, TypeError, ValueError):
                continue
        return out


def _get_exp_store() -> Any:
    """惰性初始化经验库；临时故障冷却后自动重试。"""
    global _exp_col, _exp_ready, _exp_failed_at
    if _exp_ready is True:
        return _exp_col
    if _exp_ready is False and time.monotonic() - _exp_failed_at < _STORE_RETRY_SECONDS:
        return None
    backend = (settings.vector_backend or "chroma").lower()
    try:
        _exp_col = _MilvusExperienceStore() if backend == "milvus" else _ChromaExperienceStore()
        _exp_ready = True
        _exp_failed_at = 0.0
        log_event("agent", "INFO", f"经验库向量后端就绪：{backend}", to_db=False)
    except Exception as exc:  # noqa: BLE001
        log_event("agent", "ERROR", f"经验库向量后端初始化失败（{backend}）：{str(exc)[:160]}", to_db=False)
        _exp_col = None
        _exp_ready = False
        _exp_failed_at = time.monotonic()
    return _exp_col if _exp_ready else None


def _index_experience(entry: ExperienceEntry) -> None:
    """把一条经验的向量写入 collection。"""
    col = _get_exp_store()
    if col is None:
        return
    vec = _embed([entry.content])
    if vec is None:
        return
    try:
        col.upsert(
            [str(entry.id)], vec,
            [{
                "user_id": entry.user_id,
                "status": entry.status,
                "tags": entry.tags_json or "",
                "market_env": entry.market_env_json or "",
            }],
            [entry.content],
        )
    except Exception:  # noqa: BLE001
        pass


def _delete_experience_vector(entry_id: int) -> None:
    col = _get_exp_store()
    if col is None:
        return
    try:
        col.delete_ids([str(entry_id)])
    except Exception:  # noqa: BLE001
        pass


def _vector_search_entries(query: str, user_id: int, n: int) -> list[tuple[int, float]]:
    """向量语义检索 active 经验，返回 [(entry_id, similarity), ...]，分越高越相关。"""
    col = _get_exp_store()
    if col is None:
        return []
    vec = _embed([query])
    if vec is None:
        return []
    try:
        return col.query(vec, n, "active", user_id)
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# 经验写入（复盘沉淀 / 手动添加）
# ---------------------------------------------------------------------------


def add_experience(
    db: Session,
    *,
    user_id: int,
    content: str,
    tags: list[str] | None = None,
    market_env: list[str] | None = None,
    sample_size: int = 0,
    statistical_strength: float = 0.0,
    source_review_id: int | None = None,
    active: bool = False,
) -> ExperienceEntry:
    """新增一条经验。默认 observing 观察模式；active=True 直接激活（如手动录入可信经验）。"""
    entry = ExperienceEntry(
        user_id=user_id,
        content=content.strip()[:2000],
        tags_json=json.dumps(tags or [], ensure_ascii=False),
        market_env_json=json.dumps(market_env or [], ensure_ascii=False),
        sample_size=max(0, int(sample_size)),
        statistical_strength=max(0.0, min(1.0, float(statistical_strength))),
        status="active" if active else "observing",
        source_review_id=source_review_id,
    )
    db.add(entry)
    db.flush()
    _index_experience(entry)
    db.commit()
    log_event("agent", "INFO", f"经验条目新增 id={entry.id} status={entry.status}", to_db=False)
    return entry


def activate_experience(db: Session, entry_id: int, *, user_id: int) -> bool:
    """观察期满或人工放行：observing → active，重建向量索引（status 变了要重写 metadata）。"""
    entry = db.scalar(
        select(ExperienceEntry).where(
            ExperienceEntry.id == entry_id,
            ExperienceEntry.user_id == user_id,
        ).limit(1)
    )
    if entry is None or entry.status != "observing":
        return False
    entry.status = "active"
    entry.activated_at = datetime.now()
    _index_experience(entry)  # status 变了，重写向量 metadata
    db.commit()
    return True


def archive_experience(db: Session, entry_id: int, *, user_id: int) -> bool:
    """手动归档（长期无效自动降权也走此路径）。"""
    entry = db.scalar(
        select(ExperienceEntry).where(
            ExperienceEntry.id == entry_id,
            ExperienceEntry.user_id == user_id,
        ).limit(1)
    )
    if entry is None or entry.status == "archived":
        return False
    entry.status = "archived"
    _delete_experience_vector(entry_id)  # 归档后不再被向量检索召回
    db.commit()
    return True


def archive_stale(db: Session) -> int:
    """自动降权：注入次数 ≥ 阈值且平均注入回报为负 → archived。

    成绩回填依赖 L3 台账；L3 未上线前 avg_post_injection_return 为空，此函数暂不归档任何条目，
    接口已就位，L3 上线后自动生效。
    """
    count = 0
    candidates = db.scalars(
        select(ExperienceEntry).where(
            ExperienceEntry.status == "active",
            ExperienceEntry.injected_count >= ARCHIVE_INJECT_THRESHOLD,
        )
    ).all()
    for entry in candidates:
        avg = entry.avg_post_injection_return
        if avg is not None and avg < ARCHIVE_NEG_RETURN:
            entry.status = "archived"
            _delete_experience_vector(entry.id)
            count += 1
    if count:
        db.commit()
    return count


# ---------------------------------------------------------------------------
# 检索 + 注入（分析师工作参考）
# ---------------------------------------------------------------------------


def _market_env_matches(entry: ExperienceEntry, target_env: str | None) -> bool:
    """市场环境过滤：target_env 为 None 时不过滤；否则要求经验的市场环境标签包含目标。"""
    if not target_env:
        return True
    try:
        envs = json.loads(entry.market_env_json or "[]")
    except json.JSONDecodeError:
        envs = []
    return target_env in envs


def retrieve_experiences(
    db: Session,
    *,
    user_id: int,
    query: str,
    market_env: str | None = None,
    tags: list[str] | None = None,
    limit: int = EXPERIENCE_INJECT_LIMIT,
) -> str:
    """检索与当前查询相关的经验，返回可直接注入 system prompt 的文本块；同时记注入留痕。

    检索路径：向量召回（status=active）→ 市场环境过滤 → rerank 精排 → 取 top `limit`。
    未配置 rerank 时回退向量相似度排序。
    """
    rerank_on = all(_resolve_rerank_cfg())
    pool_n = EXPERIENCE_RERANK_POOL if rerank_on else limit
    hits = _vector_search_entries(query, user_id, pool_n)
    if not hits:
        return ""

    entries: list[ExperienceEntry] = []
    for entry_id, sim in hits:
        entry = db.get(ExperienceEntry, entry_id)
        if entry is None or entry.user_id != user_id or entry.status != "active":
            continue
        if not _market_env_matches(entry, market_env):
            continue
        if tags:
            try:
                entry_tags = json.loads(entry.tags_json or "[]")
            except json.JSONDecodeError:
                entry_tags = []
            if not any(t in entry_tags for t in tags):
                continue
        entries.append(entry)
        if len(entries) >= EXPERIENCE_RERANK_POOL:
            break

    if not entries:
        return ""

    if rerank_on and len(entries) > 1:
        scored = _rerank_scores(query, [e.content for e in entries], top_n=limit)
        if scored:
            entries = [entries[i] for i, _ in scored[:limit]]
        else:
            entries = entries[:limit]
    else:
        entries = entries[:limit]

    # 注入留痕（decision_id / post_return 待 L3 台账上线后回填）
    lines = ["【历史经验参考（带样本量；统计强度低者请审慎采用）】"]
    for entry in entries:
        lines.append(
            f"- [样本N={entry.sample_size} 强度={entry.statistical_strength:.2f}] {entry.content}"
        )
        try:
            db.add(ExperienceInjection(
                user_id=user_id,
                entry_id=entry.id,
                relevance_score=None,
            ))
            entry.injected_count = (entry.injected_count or 0) + 1
        except Exception:  # noqa: BLE001
            pass
    db.commit()

    block = "\n".join(lines)
    if len(block) > EXPERIENCE_TOKEN_BUDGET:
        block = block[:EXPERIENCE_TOKEN_BUDGET].rsplit("\n", 1)[0]
    return block


def backfill_injection(
    db: Session,
    injection_id: int,
    post_return: float,
    decision_id: str | None = None,
    *,
    user_id: int,
) -> bool:
    """成绩回填：L3 台账结算后，把注入后该决策的超额收益回填到注入留痕，并刷新经验条目平均回报。

    L3 未上线前此函数不会被调用（无数据源），接口已就位。
    """
    inj = db.scalar(
        select(ExperienceInjection).where(
            ExperienceInjection.id == injection_id,
            ExperienceInjection.user_id == user_id,
        ).limit(1)
    )
    if inj is None:
        return False
    inj.post_return = float(post_return)
    if decision_id:
        inj.decision_id = decision_id
    # 刷新经验条目平均回报
    entry = db.scalar(
        select(ExperienceEntry).where(
            ExperienceEntry.id == inj.entry_id,
            ExperienceEntry.user_id == user_id,
        ).limit(1)
    )
    if entry is not None:
        all_injs = db.scalars(
            select(ExperienceInjection).where(
                ExperienceInjection.entry_id == entry.id,
                ExperienceInjection.user_id == user_id,
                ExperienceInjection.post_return.is_not(None),
            )
        ).all()
        if all_injs:
            entry.avg_post_injection_return = sum(i.post_return for i in all_injs) / len(all_injs)
    db.commit()
    return True


def activate_due(db: Session) -> int:
    """观察期满激活：observing 且创建满 OBSERVE_DAYS 天 → active。

    方案 6.4：新条目观察模式一个月再放行。可由调度器日终调用。
    """
    cutoff = datetime.now() - timedelta(days=OBSERVE_DAYS)
    candidates = db.scalars(
        select(ExperienceEntry).where(
            ExperienceEntry.status == "observing",
            ExperienceEntry.created_at <= cutoff,
        )
    ).all()
    count = 0
    for entry in candidates:
        entry.status = "active"
        entry.activated_at = datetime.now()
        _index_experience(entry)
        count += 1
    if count:
        db.commit()
    return count


def rebuild_experience_index(db: Session) -> int:
    """从 MySQL 全量重建经验向量索引（切换后端 / 修复时用）。"""
    col = _get_exp_store()
    if col is None:
        return -1
    entries = db.scalars(
        select(ExperienceEntry).where(
            ExperienceEntry.user_id.is_not(None),
            ExperienceEntry.status != "archived",
        )
    ).all()
    if not entries:
        return 0
    try:
        col.delete_status("active")
        col.delete_status("observing")
    except Exception:  # noqa: BLE001
        pass
    batch = 32
    total = 0
    for i in range(0, len(entries), batch):
        chunk = entries[i:i + batch]
        vecs = _embed([e.content for e in chunk])
        if vecs is None:
            continue
        try:
            col.upsert(
                [str(e.id) for e in chunk], vecs,
                [{
                    "user_id": e.user_id,
                    "status": e.status,
                    "tags": e.tags_json or "",
                    "market_env": e.market_env_json or "",
                } for e in chunk],
                [e.content for e in chunk],
            )
            total += len(chunk)
        except Exception:  # noqa: BLE001
            pass
    log_event("agent", "INFO", f"经验向量索引重建完成 {total} 条", to_db=False)
    return total
