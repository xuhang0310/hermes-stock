"""用户长期记忆模块 — 向量语义检索 + 关键词匹配 + RRF 混合融合。

设计参考 Hermes Agent 生态（hermes-local-memory / scope-recall-hermes / ClawMem）：
  1. 原始对话 → 异步 LLM 消化 → 结构化记忆（facts/preferences/decisions/gaps/interests）
  2. 下次对话 → 向量语义检索 + 关键词搜索 → RRF 融合 → 注入 system prompt
  3. 记忆有生命周期：被引用时增强，长期未引用衰减
  4. 向量库：ChromaDB（嵌入式，跨平台，可切换 Milvus）
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import desc, or_, select, text
from sqlalchemy.orm import Session

from backend.app.core.llm_config import active_llm_config
from backend.app.core.applog import log_event
from backend.app.core.database import SessionLocal
from backend.app.core.models import AiChatMessage, AiChatSession, UserMemory
from backend.app.core.settings import settings

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DIGEST_MIN_MESSAGES = 4
DIGEST_LOOKBACK = 20
MEMORY_RETRIEVAL_LIMIT = 8
RERANK_POOL = 24  # 开启重排时，先从混合检索取这么多候选，再由 rerank 精排到 MEMORY_RETRIEVAL_LIMIT
MEMORY_CONTEXT_TOKEN_BUDGET = 800
MEMORY_TYPES = {"fact": "事实", "preference": "偏好", "decision": "决策", "gap": "缺口", "interest": "关注"}

# 向量检索权重
HYBRID_VECTOR_WEIGHT = 0.55  # 语义相似度权重
HYBRID_KEYWORD_WEIGHT = 0.45  # 关键词匹配权重

# 向量库：chroma（嵌入式，默认）| milvus（服务端），按 STOCK_VECTOR_BACKEND 切换
_CHROMA_PATH = Path(__file__).resolve().parents[2] / "chroma_data"
_COLLECTION = "user_memories"

# 延迟初始化
_embedding_fn: Any = None
_embedding_dim: int = 0
_store: Any = None
_store_ready: bool | None = None

# ---------------------------------------------------------------------------
# 嵌入函数（与向量库解耦；远程 /embeddings 优先，缺则本地降级）
# ---------------------------------------------------------------------------


def _resolve_embedding_cfg() -> tuple[str, str, str]:
    """嵌入配置解析：后台数据源(embedding) → env(STOCK_EMBEDDING_*) → 活跃 LLM。返回 (base_url, api_key, model)。"""
    base = key = model = ""
    try:
        from backend.app.core.models import DataSourceConfig
        with SessionLocal() as db:
            row = db.get(DataSourceConfig, "embedding")
            if row and (row.status or "enabled") == "enabled":
                base = (row.base_url or "").strip()
                key = (row.api_key or "").strip()
                model = (row.model or "").strip()
    except Exception:
        pass
    base = base or settings.embedding_base_url
    key = key or settings.embedding_api_key
    model = model or settings.embedding_model
    if not (base and key):
        llm = active_llm_config()
        base = base or (llm.get("base_url") or "")
        key = key or (llm.get("api_key") or "")
    return base, key, (model or "text-embedding-3-small")


def _resolve_rerank_cfg() -> tuple[str, str, str]:
    """重排配置解析：后台数据源(rerank) → env(STOCK_RERANK_MODEL)。base/key 未单独配置则复用嵌入配置；
    无模型名视为关闭重排（返回空）。返回 (base_url, api_key, model)。"""
    base = key = model = ""
    try:
        from backend.app.core.models import DataSourceConfig
        with SessionLocal() as db:
            row = db.get(DataSourceConfig, "rerank")
            if row and (row.status or "enabled") == "enabled":
                base = (row.base_url or "").strip()
                key = (row.api_key or "").strip()
                model = (row.model or "").strip()
    except Exception:
        pass
    model = model or settings.rerank_model
    if not model:
        return "", "", ""
    if not (base and key):
        emb_base, emb_key, _ = _resolve_embedding_cfg()
        base = base or emb_base
        key = key or emb_key
    return base, key, model


def _ensure_embedding():
    """延迟初始化嵌入函数（与向量库无关）。配置优先级见 _resolve_embedding_cfg；都不行再降级本地模型。"""
    global _embedding_fn, _embedding_dim
    if _embedding_fn is not None:
        return _embedding_fn

    emb_base, emb_key, emb_model = _resolve_embedding_cfg()
    if emb_base and emb_key:
        try:
            fn = _RemoteEmbeddingFunction(base_url=emb_base, api_key=emb_key, model=emb_model)
            test_vec = fn(["test"])  # 测试连通性并探测真实维度
            if test_vec and len(test_vec[0]) > 0:
                _embedding_fn = fn
                _embedding_dim = fn.dim
                log_event("agent", "INFO", f"使用远程嵌入 API（{emb_model}），维度={_embedding_dim}", to_db=False)
                return _embedding_fn
        except Exception:
            log_event("agent", "WARNING", "远程嵌入 API 不可用，降级到本地模型", to_db=False)

    try:
        from chromadb.utils import embedding_functions as ef
        _embedding_fn = ef.SentenceTransformerEmbeddingFunction(model_name="shibing624/text2vec-base-chinese")
        _embedding_dim = 768
        log_event("agent", "INFO", "使用本地嵌入模型 text2vec-base-chinese，维度=768", to_db=False)
    except Exception:
        log_event("agent", "WARNING", "嵌入模型不可用，向量检索关闭", to_db=False)
        _embedding_fn = None
        _embedding_dim = 0
    return _embedding_fn


class _RemoteEmbeddingFunction:
    """OpenAI-compatible /embeddings API 封装，同时探测维度。"""

    def __init__(self, base_url: str, api_key: str, model: str = "text-embedding-3-small"):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self.dim = 0

    def __call__(self, texts: list[str]) -> list[list[float]]:
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        multimodal = "multimodal-embedding" in self._base_url
        url = self._base_url if multimodal else self._base_url.rstrip("/") + "/embeddings"
        vectors: list[list[float]] = []
        with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            for i in range(0, len(texts), 10):  # 分块：千问 text-embedding-v4 批量上限 10，统一分块兼容各家
                batch = texts[i:i + 10]
                if multimodal:
                    # DashScope 原生多模态嵌入（qwen3-vl-embedding）：input={contents:[{text}]}，dim 对齐 Milvus 1024
                    payload = {"model": self._model, "input": {"contents": [{"text": t} for t in batch]},
                               "parameters": {"dimension": 1024}}
                    resp = client.post(url, json=payload, headers=headers)
                    if resp.status_code != 200:
                        raise RuntimeError(f"嵌入 API 返回 {resp.status_code}: {resp.text[:200]}")
                    vectors.extend(item["embedding"] for item in resp.json()["output"]["embeddings"])
                else:  # OpenAI 兼容 /embeddings（SiliconFlow bge-m3、千问 text-embedding-v4 等）
                    resp = client.post(url, json={"input": batch, "model": self._model}, headers=headers)
                    if resp.status_code != 200:
                        raise RuntimeError(f"嵌入 API 返回 {resp.status_code}: {resp.text[:200]}")
                    vectors.extend(item["embedding"] for item in resp.json()["data"])
        if self.dim == 0 and vectors:
            self.dim = len(vectors[0])
        return vectors


def _embed(texts: list[str]) -> list[list[float]] | None:
    """生成文本嵌入向量。失败返回 None。"""
    fn = _ensure_embedding()
    if fn is None:
        return None
    try:
        return fn(texts)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 向量库后端抽象：upsert / delete / delete_user / query（分越高越相关）
# ---------------------------------------------------------------------------


class _ChromaStore:
    """ChromaDB 嵌入式后端（默认；本地/Windows 开发用）。"""

    def __init__(self):
        import chromadb
        _CHROMA_PATH.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(_CHROMA_PATH))
        self._col = self._client.get_or_create_collection(name=_COLLECTION, metadata={"hnsw:space": "cosine"})

    def upsert(self, ids, embeddings, metadatas, documents):
        self._col.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas, documents=documents)

    def delete(self, ids):
        self._col.delete(ids=ids)

    def delete_user(self, user_id):
        self._col.delete(where={} if user_id is None else {"user_id": user_id})

    def query(self, embedding, user_id, n):
        res = self._col.query(query_embeddings=embedding, n_results=n, where={"user_id": user_id}, include=["distances"])
        ids = res.get("ids", [[]])[0]
        dists = res.get("distances", [[]])[0]
        out = []
        for did, dist in zip(ids, dists):
            try:  # cosine distance ∈ [0,2] → similarity ∈ [0,1]
                out.append((int(did), max(0.0, 1.0 - float(dist) / 2.0)))
            except (ValueError, TypeError):
                continue
        return out


class _MilvusStore:
    """Milvus 服务端后端（生产；STOCK_MILVUS_URI）。user_id 显式建字段可过滤。"""

    def __init__(self):
        from pymilvus import MilvusClient, DataType
        if _ensure_embedding() is None or not _embedding_dim:
            raise RuntimeError("嵌入维度未知，无法建 Milvus collection")
        self._dim = _embedding_dim
        self._c = MilvusClient(uri=settings.milvus_uri, token=settings.milvus_token or "")
        if not self._c.has_collection(_COLLECTION):
            schema = self._c.create_schema(auto_id=False, enable_dynamic_field=True)
            schema.add_field("id", DataType.INT64, is_primary=True)
            schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self._dim)
            schema.add_field("user_id", DataType.INT64)
            idx = self._c.prepare_index_params()
            idx.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
            self._c.create_collection(collection_name=_COLLECTION, schema=schema, index_params=idx)
        self._c.load_collection(_COLLECTION)

    def upsert(self, ids, embeddings, metadatas, documents):
        data = [
            {"id": int(i), "vector": v, "user_id": int((meta or {}).get("user_id", 0)), "content": doc}
            for i, v, meta, doc in zip(ids, embeddings, metadatas, documents)
        ]
        self._c.upsert(collection_name=_COLLECTION, data=data)

    def delete(self, ids):
        self._c.delete(collection_name=_COLLECTION, ids=[int(i) for i in ids])

    def delete_user(self, user_id):
        flt = "id >= 0" if user_id is None else f"user_id == {int(user_id)}"
        self._c.delete(collection_name=_COLLECTION, filter=flt)

    def query(self, embedding, user_id, n):
        res = self._c.search(
            collection_name=_COLLECTION, data=embedding, limit=n,
            filter=f"user_id == {int(user_id)}", output_fields=[],
            consistency_level="Strong",  # 刚写入的记忆需立即可检索（与 chroma 一致）
        )
        out = []
        for hit in (res[0] if res else []):
            try:  # Milvus COSINE 的 distance 即相似度（越大越近）
                out.append((int(hit["id"]), float(hit.get("distance", 0.0))))
            except (ValueError, TypeError, KeyError):
                continue
        return out


def _get_store():
    """按 STOCK_VECTOR_BACKEND 惰性初始化向量库后端；失败优雅降级（返回 None）。"""
    global _store, _store_ready
    if _store_ready is not None:
        return _store if _store_ready else None
    backend = (settings.vector_backend or "chroma").lower()
    try:
        _store = _MilvusStore() if backend == "milvus" else _ChromaStore()
        _store_ready = True
        log_event("agent", "INFO", f"向量库后端就绪：{backend}", to_db=False)
    except Exception as exc:  # noqa: BLE001
        log_event("agent", "ERROR", f"向量库后端初始化失败（{backend}）：{str(exc)[:160]}", to_db=False)
        _store = None
        _store_ready = False
    return _store


# ---------------------------------------------------------------------------
# 向量索引（记忆写入/更新时调用）
# ---------------------------------------------------------------------------


def _index_memory(row: UserMemory) -> None:
    """将一条记忆的向量写入向量库。"""
    store = _get_store()
    if store is None:
        return
    vec = _embed([row.content])
    if vec is None:
        return
    try:
        store.upsert(
            [str(row.id)], vec,
            [{"user_id": row.user_id, "memory_type": row.memory_type, "importance": row.importance}],
            [row.content],
        )
    except Exception:
        pass


def _delete_memory_vector(memory_id: int) -> None:
    """从向量库删除一条记忆的向量。"""
    store = _get_store()
    if store is None:
        return
    try:
        store.delete([str(memory_id)])
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 混合检索：向量语义 + 关键词匹配 → RRF 融合
# ---------------------------------------------------------------------------


def _rerank_scores(query: str, texts: list[str], top_n: int) -> list[tuple[int, float]] | None:
    """调 rerank API 精排：返回 [(原列表下标, 相关性分)]（API 已按相关性降序）。

    未配置 / 候选不足 / 调用失败返回 None，调用方回退融合排序。
    记忆检索与 RAG 知识库检索（ai/retrieval.py）共用本函数。
    """
    if len(texts) <= 1:
        return None
    base, key, model = _resolve_rerank_cfg()
    if not (base and key and model):
        return None
    try:
        b = base.rstrip("/")  # SiliconFlow 用 /rerank；千问 compatible-api 用完整 /reranks 路径
        url = b if b.endswith(("/rerank", "/reranks")) else b + "/rerank"
        payload = {"model": model, "query": query, "documents": texts, "top_n": top_n}
        headers = {"Authorization": f"Bearer {key}"}
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
            resp = client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(f"rerank {resp.status_code}: {resp.text[:150]}")
            results = resp.json().get("results", [])
        scored = [
            (r["index"], float(r.get("relevance_score", 0.0)))
            for r in results
            if isinstance(r.get("index"), int) and 0 <= r["index"] < len(texts)
        ]
        return scored or None
    except Exception:
        log_event("agent", "WARNING", "重排不可用，回退融合排序", to_db=False)
        return None


def _rerank(query: str, memories: list[UserMemory], top_n: int) -> list[UserMemory]:
    """用 rerank 模型对候选记忆按与 query 的相关性精排；任何失败都回退原融合排序。"""
    scored = _rerank_scores(query, [m.content for m in memories], top_n)
    if not scored:
        return memories[:top_n]
    return [memories[index] for index, _ in scored][:top_n]


def retrieve_memories(
    db: Session,
    *,
    user_id: int,
    query: str,
    digital_human_id: int | None = None,
    limit: int = MEMORY_RETRIEVAL_LIMIT,
) -> str:
    """检索与当前查询相关的用户记忆，返回可直接注入 system prompt 的文本块。"""
    rerank_on = all(_resolve_rerank_cfg())
    # 开启重排时先召回更大的候选池（RERANK_POOL），再精排到 limit
    pool = _hybrid_search(
        db, user_id=user_id, query=query, digital_human_id=digital_human_id,
        limit=RERANK_POOL if rerank_on else limit,
    )
    if not pool:
        return ""
    memories = _rerank(query, pool, top_n=limit) if rerank_on else pool[:limit]
    if not memories:
        return ""

    lines = ["【用户长期记忆】"]
    for m in memories:
        type_label = MEMORY_TYPES.get(m.memory_type, m.memory_type)
        lines.append(f"- [{type_label}] {m.content}")
        m.access_count = (m.access_count or 0) + 1
        m.last_accessed_at = datetime.now()

    db.commit()
    block = "\n".join(lines)
    if len(block) > MEMORY_CONTEXT_TOKEN_BUDGET:
        block = block[:MEMORY_CONTEXT_TOKEN_BUDGET].rsplit("\n", 1)[0]
    return block


def _hybrid_search(
    db: Session,
    *,
    user_id: int,
    query: str,
    digital_human_id: int | None = None,
    limit: int = MEMORY_RETRIEVAL_LIMIT,
) -> list[UserMemory]:
    """混合检索：向量语义 + 关键词匹配 → RRF 融合排序。"""
    # 获取候选池（关键词匹配 + 向量检索各取 limit*2 条）
    keyword_results = _keyword_search(db, user_id=user_id, query=query, digital_human_id=digital_human_id, limit=limit * 3)
    vector_results = _vector_search(query=query, user_id=user_id, limit=limit * 3)

    # 构建 ID → 记忆映射 和 RRF 分数
    memory_map: dict[int, UserMemory] = {}
    rrf_scores: dict[int, float] = {}

    # 关键词结果
    for rank, m in enumerate(keyword_results):
        memory_map[m.id] = m
        rrf = 1.0 / (60 + rank + 1)  # RRF: k=60
        rrf_scores[m.id] = rrf * HYBRID_KEYWORD_WEIGHT

    # 向量结果
    for rank, (mem_id, vec_score) in enumerate(vector_results):
        rrf = 1.0 / (60 + rank + 1)
        rrf_scores[mem_id] = rrf_scores.get(mem_id, 0) + rrf * HYBRID_VECTOR_WEIGHT
        if mem_id not in memory_map:
            m = db.get(UserMemory, mem_id)
            if m and m.user_id == user_id and m.status == "active":
                memory_map[mem_id] = m

    # 对仅有向量命中但没有关键词命中的记忆，也加入评分
    for m in keyword_results:
        if m.id not in memory_map:
            memory_map[m.id] = m

    # 复合分 = RRF 分数 + 重要性加成 + 时效加成
    now = datetime.now()
    composite: list[tuple[float, UserMemory]] = []
    for mem_id, rrf in rrf_scores.items():
        m = memory_map.get(mem_id)
        if m is None:
            continue
        age_days = (now - (m.updated_at or m.created_at)).days
        recency = max(0, 1.0 - age_days / 30.0)
        score = rrf + m.importance * 0.15 + recency * 0.1
        composite.append((score, m))

    composite.sort(key=lambda x: x[0], reverse=True)
    return [m for _, m in composite[:limit]]


def _keyword_search(
    db: Session,
    *,
    user_id: int,
    query: str,
    digital_human_id: int | None = None,
    limit: int = MEMORY_RETRIEVAL_LIMIT,
) -> list[UserMemory]:
    """基于 SQL LIKE 的关键词搜索。"""
    keywords = _extract_keywords(query)
    filters = [UserMemory.user_id == user_id, UserMemory.status == "active"]
    if digital_human_id:
        filters.append(or_(UserMemory.digital_human_id.is_(None), UserMemory.digital_human_id == digital_human_id))

    if not keywords:
        return db.scalars(
            select(UserMemory).where(*filters)
            .order_by(desc(UserMemory.importance), desc(UserMemory.updated_at))
            .limit(limit)
        ).all()

    like_filters = [UserMemory.content.like(f"%{kw}%") for kw in keywords[:5]]
    filters.append(or_(*like_filters))
    return db.scalars(
        select(UserMemory).where(*filters)
        .order_by(desc(UserMemory.importance), desc(UserMemory.updated_at))
        .limit(limit)
    ).all()


def _vector_search(
    *,
    query: str,
    user_id: int,
    limit: int = MEMORY_RETRIEVAL_LIMIT,
) -> list[tuple[int, float]]:
    """向量语义搜索（后端无关）。返回 [(memory_id, score), ...]，分越高越相关。"""
    store = _get_store()
    if store is None:
        return []
    vec = _embed([query])
    if vec is None:
        return []
    try:
        return store.query(vec, user_id, limit)
    except Exception:
        return []


def _extract_keywords(query: str) -> list[str]:
    """从用户查询中提取关键词，过滤停用词。"""
    stopwords = {
        "帮我", "看看", "一下", "这个", "那个", "怎么", "什么", "为什么",
        "可以", "吗", "呢", "吧", "啊", "的", "了", "在", "是", "有",
        "和", "与", "或", "把", "被", "从", "对", "到", "一个", "现在",
        "分析", "查询", "显示", "列出", "生成", "检查", "扫描",
    }
    tokens = re.findall(r"[一-鿿]{2,}|[A-Za-z0-9]{2,}", query)
    keywords: list[str] = []
    for tok in tokens:
        if tok.lower() not in stopwords and len(tok) >= 2:
            keywords.append(tok)
    seen: set[str] = set()
    result: list[str] = []
    for kw in keywords:
        key = kw.lower()
        if key not in seen:
            seen.add(key)
            result.append(kw)
            if len(result) >= 5:
                break
    return result


# ---------------------------------------------------------------------------
# 记忆消化（对话结束后异步调用）
# ---------------------------------------------------------------------------


def digest_conversation(
    db: Session,
    *,
    session_id: int,
    user_id: int,
    digital_human_id: int | None = None,
) -> int:
    """从对话中提取结构化记忆并写入 DB + 向量索引。返回新增/更新的记忆数。"""
    messages = db.scalars(
        select(AiChatMessage)
        .where(AiChatMessage.session_id == session_id, AiChatMessage.error.is_(None))
        .order_by(AiChatMessage.id.desc())
        .limit(DIGEST_LOOKBACK)
    ).all()
    messages.reverse()

    user_messages = [m for m in messages if m.role == "user"]
    if len(user_messages) < DIGEST_MIN_MESSAGES:
        return 0

    # 同一 session 不重复消化
    evidence_prefix = f"session:{session_id}"
    existing = db.scalar(
        select(UserMemory).where(
            UserMemory.user_id == user_id,
            UserMemory.evidence.like(f"{evidence_prefix}%"),
        ).limit(1)
    )
    if existing is not None:
        return 0

    # 调 LLM 提取
    try:
        extracted = _call_llm_for_digestion(messages, user_id)
    except Exception:
        log_event("agent", "WARNING", f"记忆消化 LLM 调用失败 session:{session_id}", to_db=False)
        return 0

    if not extracted:
        return 0

    count = 0
    now = datetime.now()
    evidence = f"{evidence_prefix}, {now.strftime('%Y-%m-%d')}"

    for item in extracted:
        memory_type = item.get("type", "fact")
        if memory_type not in MEMORY_TYPES:
            memory_type = "fact"
        content = str(item.get("content", "")).strip()
        if not content:
            continue

        confidence = min(1.0, max(0.1, float(item.get("confidence", 0.7))))
        importance = min(1.0, max(0.1, float(item.get("importance", 0.5))))

        # 检查重复
        if _merge_similar(db, user_id=user_id, content=content, memory_type=memory_type, confidence=confidence, evidence=evidence):
            count += 1
            continue

        row = UserMemory(
            user_id=user_id,
            digital_human_id=digital_human_id,
            memory_type=memory_type,
            content=content[:600],
            evidence=evidence,
            importance=importance,
            confidence=confidence,
            status="active",
        )
        db.add(row)
        db.flush()  # 取得 row.id
        _index_memory(row)
        count += 1

    db.commit()
    if count:
        log_event("agent", "INFO", f"记忆消化完成 session:{session_id} 新增/更新 {count} 条", to_db=False)
    return count


def _merge_similar(
    db: Session,
    *,
    user_id: int,
    content: str,
    memory_type: str,
    confidence: float,
    evidence: str,
) -> bool:
    """同内容同类型的记忆合并，更新置信度和向量索引。"""
    existing = db.scalar(
        select(UserMemory).where(
            UserMemory.user_id == user_id,
            UserMemory.memory_type == memory_type,
            UserMemory.content == content,
            UserMemory.status == "active",
        ).limit(1)
    )
    if existing is None:
        return False

    existing.confidence = (existing.confidence + confidence) / 2
    existing.importance = max(existing.importance, 0.5)
    existing.evidence = evidence
    existing.updated_at = datetime.now()
    _index_memory(existing)
    return True


def _call_llm_for_digestion(messages: list[AiChatMessage], user_id: int) -> list[dict[str, Any]]:
    """调用 LLM 从对话消息中提取结构化记忆。"""
    llm_config = active_llm_config()
    if not llm_config["api_key"] or not llm_config["base_url"]:
        raise RuntimeError("LLM 未配置")

    convo_lines: list[str] = []
    for msg in messages[-16:]:
        role_label = "用户" if msg.role == "user" else "助手"
        snippet = (msg.content or "")[:400]
        convo_lines.append(f"[{role_label}] {snippet}")

    system_prompt = (
        "你是一个用户记忆提取器。分析对话，提取关于用户的事实、偏好、决策、知识缺口和关注主题。\n"
        "输出严格的 JSON 数组，每条记忆包含：\n"
        '  type: "fact"|"preference"|"decision"|"gap"|"interest"\n'
        "  content: 中文描述，简洁准确，不超过150字\n"
        "  confidence: 0.0-1.0，你对这条记忆的确信程度\n"
        "  importance: 0.0-1.0，这条记忆对理解用户的重要性\n\n"
        "规则：\n"
        "- fact: 可验证的事实，如持仓、交易动作\n"
        "- preference: 用户表达的方法、风格、习惯偏好\n"
        "- decision: 用户明确的决定或结论\n"
        "- gap: 用户关心但尚未获得满意答案的问题\n"
        "- interest: 用户反复提及的话题或标的\n"
        "- 只提取有价值的、能帮助未来对话的信息\n"
        "- 不要提取临时性、一次性的琐碎问题\n"
        "- 如果对话中没有值得长期记住的内容，返回空数组 []\n"
        "- 只输出 JSON，不要其他文字"
    )

    user_message = (
        f"以下是一个股票研究平台用户与AI助手的对话，请提取用户记忆：\n\n"
        f"{chr(10).join(convo_lines)}\n\n"
        f"输出 JSON 数组："
        f'[{{"type":"...","content":"...","confidence":0.8,"importance":0.6}}]'
    )

    url = llm_config["base_url"].rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": llm_config["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {llm_config['api_key']}"}

    with httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
        response = client.post(url, json=payload, headers=headers)
        if response.status_code != 200:
            raise RuntimeError(f"LLM 返回 {response.status_code}")

    data = response.json()
    content = data["choices"][0]["message"]["content"]
    return _extract_json_array(content)


def _extract_json_array(text: str) -> list[dict[str, Any]]:
    """从 LLM 响应中提取 JSON 数组。"""
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            items = parsed.get("memories") or parsed.get("items") or []
            if isinstance(items, list):
                return items
    except json.JSONDecodeError:
        pass

    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        try:
            parsed = json.loads(m.group(1).strip())
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    return []


# ---------------------------------------------------------------------------
# 记忆维护
# ---------------------------------------------------------------------------


def rebuild_vector_index(db: Session, user_id: int | None = None) -> int:
    """从 MySQL 全量重新嵌入并重建向量索引（切换后端 / 迁移到 Milvus 时用）。"""
    store = _get_store()
    if store is None:
        return -1

    filters = [UserMemory.status == "active"]
    if user_id:
        filters.append(UserMemory.user_id == user_id)

    memories = db.scalars(select(UserMemory).where(*filters).order_by(UserMemory.id)).all()
    if not memories:
        return 0

    try:  # 清空对应范围再重建
        store.delete_user(user_id)
    except Exception:
        pass

    batch_size = 32
    total = 0
    for i in range(0, len(memories), batch_size):
        batch = memories[i:i + batch_size]
        texts = [m.content for m in batch]
        vecs = _embed(texts)
        if vecs is None:
            continue
        try:
            store.upsert(
                [str(m.id) for m in batch], vecs,
                [{"user_id": m.user_id, "memory_type": m.memory_type, "importance": m.importance} for m in batch],
                texts,
            )
            total += len(batch)
        except Exception:
            pass

    log_event("agent", "INFO", f"向量索引重建完成 {total} 条记忆", to_db=False)
    return total


# ---------------------------------------------------------------------------
# 异步调度
# ---------------------------------------------------------------------------


async def schedule_memory_digestion(session_id: int, user_id: int, digital_human_id: int | None = None) -> None:
    """异步调度记忆消化任务。不阻塞对话响应。"""
    try:
        await asyncio.to_thread(_digest_in_new_session, session_id, user_id, digital_human_id)
    except Exception:
        pass


def _digest_in_new_session(session_id: int, user_id: int, digital_human_id: int | None) -> None:
    with SessionLocal() as db:
        digest_conversation(db, session_id=session_id, user_id=user_id, digital_human_id=digital_human_id)
