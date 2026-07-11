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

from backend.app.agents.stock_agent import _active_llm_config
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

# ChromaDB 路径
_CHROMA_PATH = Path(__file__).resolve().parents[2] / "chroma_data"

# 延迟初始化
_chroma_client: Any = None
_embedding_fn: Any = None
_embedding_dim: int = 0

# ---------------------------------------------------------------------------
# 嵌入 & 向量库初始化
# ---------------------------------------------------------------------------


def _ensure_chroma() -> tuple[Any, Any, int]:
    """延迟初始化 ChromaDB 客户端和嵌入函数。返回 (client, embedding_fn, dim)。"""
    global _chroma_client, _embedding_fn, _embedding_dim
    if _chroma_client is not None:
        return _chroma_client, _embedding_fn, _embedding_dim

    try:
        import chromadb
        from chromadb.utils import embedding_functions as ef

        _CHROMA_PATH.mkdir(parents=True, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(path=str(_CHROMA_PATH))

        # 优先用独立配置的 embeddings 服务（与聊天模型解耦，见 STOCK_EMBEDDING_*）；
        # 未配置时回退到活跃 LLM 提供商的 /embeddings。
        emb_base = settings.embedding_base_url
        emb_key = settings.embedding_api_key
        emb_model = settings.embedding_model or "text-embedding-3-small"
        if not (emb_base and emb_key):
            llm_config = _active_llm_config()
            emb_base = llm_config.get("base_url") or ""
            emb_key = llm_config.get("api_key") or ""
        if emb_base and emb_key:
            try:
                _embedding_fn = _RemoteEmbeddingFunction(base_url=emb_base, api_key=emb_key, model=emb_model)
                # 测试连通性（同时探测真实维度）
                test_vec = _embedding_fn(["test"])
                if test_vec and len(test_vec[0]) > 0:
                    _embedding_dim = _embedding_fn.dim
                    log_event("agent", "INFO", f"使用远程嵌入 API（{emb_model}），维度={_embedding_dim}", to_db=False)
                    return _chroma_client, _embedding_fn, _embedding_dim
            except Exception:
                log_event("agent", "WARNING", "远程嵌入 API 不可用，降级到本地模型", to_db=False)

        # 降级：本地 sentence-transformers
        _embedding_fn = ef.SentenceTransformerEmbeddingFunction(
            model_name="shibing624/text2vec-base-chinese",
        )
        _embedding_dim = 768
        log_event("agent", "INFO", f"使用本地嵌入模型 text2vec-base-chinese，维度={_embedding_dim}", to_db=False)

    except ImportError:
        log_event("agent", "WARNING", "chromadb 未安装，向量检索不可用", to_db=False)
        _chroma_client = False  # 标记为不可用
        _embedding_fn = None
        _embedding_dim = 0
    except Exception:
        log_event("agent", "ERROR", "ChromaDB 初始化失败", to_db=False)
        _chroma_client = False
        _embedding_fn = None
        _embedding_dim = 0

    return _chroma_client, _embedding_fn, _embedding_dim


class _RemoteEmbeddingFunction:
    """OpenAI-compatible /embeddings API 封装，同时探测维度。"""

    def __init__(self, base_url: str, api_key: str, model: str = "text-embedding-3-small"):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self.dim = 0

    def __call__(self, texts: list[str]) -> list[list[float]]:
        url = self._base_url + "/embeddings"
        payload = {"input": texts, "model": self._model}
        headers = {"Authorization": f"Bearer {self._api_key}"}
        with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            resp = client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(f"Embeddings API 返回 {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            vectors = [item["embedding"] for item in data["data"]]
            if self.dim == 0 and vectors:
                self.dim = len(vectors[0])
            return vectors


def _embed(texts: list[str]) -> list[list[float]] | None:
    """生成文本嵌入向量。失败返回 None。"""
    _, ef, _ = _ensure_chroma()
    if ef is None:
        return None
    try:
        return ef(texts)
    except Exception:
        return None


def _get_collection():
    """获取或创建 ChromaDB collection。"""
    client, ef, dim = _ensure_chroma()
    if client is False or ef is None:
        return None
    try:
        return client.get_or_create_collection(
            name="user_memories",
            metadata={"hnsw:space": "cosine"},
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 向量索引（记忆写入/更新时调用）
# ---------------------------------------------------------------------------


def _index_memory(row: UserMemory) -> None:
    """将一条记忆的向量写入 ChromaDB。"""
    collection = _get_collection()
    if collection is None:
        return
    vec = _embed([row.content])
    if vec is None:
        return
    try:
        collection.upsert(
            ids=[str(row.id)],
            embeddings=vec,
            metadatas=[{"user_id": row.user_id, "memory_type": row.memory_type, "importance": row.importance}],
            documents=[row.content],
        )
    except Exception:
        pass


def _delete_memory_vector(memory_id: int) -> None:
    """从 ChromaDB 中删除一条记忆的向量。"""
    collection = _get_collection()
    if collection is None:
        return
    try:
        collection.delete(ids=[str(memory_id)])
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 混合检索：向量语义 + 关键词匹配 → RRF 融合
# ---------------------------------------------------------------------------


def _rerank(query: str, memories: list[UserMemory], top_n: int) -> list[UserMemory]:
    """用 rerank 模型对候选记忆按与 query 的相关性精排；任何失败都回退原融合排序。"""
    if len(memories) <= 1:
        return memories[:top_n]
    try:
        docs = [m.content for m in memories]
        url = settings.embedding_base_url.rstrip("/") + "/rerank"
        payload = {"model": settings.rerank_model, "query": query, "documents": docs, "top_n": top_n}
        headers = {"Authorization": f"Bearer {settings.embedding_api_key}"}
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
            resp = client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(f"rerank {resp.status_code}: {resp.text[:150]}")
            results = resp.json().get("results", [])
        ordered = [
            memories[r["index"]]
            for r in results
            if isinstance(r.get("index"), int) and 0 <= r["index"] < len(memories)
        ]
        return ordered[:top_n] if ordered else memories[:top_n]
    except Exception:
        log_event("agent", "WARNING", "记忆重排不可用，回退融合排序", to_db=False)
        return memories[:top_n]


def retrieve_memories(
    db: Session,
    *,
    user_id: int,
    query: str,
    digital_human_id: int | None = None,
    limit: int = MEMORY_RETRIEVAL_LIMIT,
) -> str:
    """检索与当前查询相关的用户记忆，返回可直接注入 system prompt 的文本块。"""
    rerank_on = bool(settings.rerank_model and settings.embedding_base_url and settings.embedding_api_key)
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
    """基于 ChromaDB 的向量语义搜索。返回 [(memory_id, score), ...]."""
    collection = _get_collection()
    if collection is None:
        return []

    vec = _embed([query])
    if vec is None:
        return []

    try:
        results = collection.query(
            query_embeddings=vec,
            n_results=limit,
            where={"user_id": user_id},
            include=["metadatas", "distances"],
        )
    except Exception:
        return []

    ids = results.get("ids", [[]])[0]
    distances = results.get("distances", [[]])[0]

    # 余弦距离转相似度分数（ChromaDB 默认用 cosine distance）
    scored: list[tuple[int, float]] = []
    for doc_id, distance in zip(ids, distances):
        try:
            mem_id = int(doc_id)
        except (ValueError, TypeError):
            continue
        # cosine distance ∈ [0, 2]，转成 similarity ∈ [0, 1]
        sim = max(0.0, 1.0 - distance / 2.0)
        scored.append((mem_id, sim))
    return scored


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
    llm_config = _active_llm_config()
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
    """重建 ChromaDB 向量索引（从 MySQL 全量重新嵌入）。"""
    collection = _get_collection()
    if collection is None:
        return -1

    filters = [UserMemory.status == "active"]
    if user_id:
        filters.append(UserMemory.user_id == user_id)

    memories = db.scalars(select(UserMemory).where(*filters).order_by(UserMemory.id)).all()
    if not memories:
        return 0

    # 清空并重建
    try:
        collection.delete(where={} if user_id is None else {"user_id": user_id})
    except Exception:
        pass

    batch_size = 20
    total = 0
    for i in range(0, len(memories), batch_size):
        batch = memories[i:i + batch_size]
        texts = [m.content for m in batch]
        vecs = _embed(texts)
        if vecs is None:
            continue
        try:
            collection.upsert(
                ids=[str(m.id) for m in batch],
                embeddings=vecs,
                metadatas=[{"user_id": m.user_id, "memory_type": m.memory_type, "importance": m.importance} for m in batch],
                documents=texts,
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
