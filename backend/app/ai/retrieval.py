"""RAG 通用检索器（P1）：向量 + 关键词双召回 → RRF 融合 → rerank 精排，带四路分数返回。

单一职责：只负责"查"；入库在 ai/rag_ingest.py，HTTP 编排在 ai/ai_api.py。

复用（DRY）：嵌入 _embed、拆词 _extract_keywords、精排 _rerank_scores、融合权重
HYBRID_VECTOR_WEIGHT/HYBRID_KEYWORD_WEIGHT 全部 import 自 memory；RRF 公式与
memory._hybrid_search 同源（k=60）。不复用 _hybrid_search 本身——它硬耦合 UserMemory 表。

过滤（奥卡姆）：统一在 SQL JOIN 做（doc_type/trust_level/symbol 一处过滤），
向量库 query 不带过滤参数，避免给 chroma/milvus 各写一套表达式。
"""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.app.ai import memory as _memory
from backend.app.ai.query_rewrite import rewrite_and_expand
from backend.app.ai.rag_ingest import get_store
from backend.app.core.models import Document, DocumentChunk, DocumentSymbol
from backend.app.core.settings import settings

_RRF_K = 60  # 与 memory._hybrid_search 相同


def retrieve_chunks(
    db: Session,
    query: str,
    *,
    top_k: int | None = None,
    symbol: str | None = None,
    doc_types: list[str] | None = None,
    trust_levels: list[str] | None = None,
) -> list[dict]:
    """检索知识库切块，返回带 vector/keyword/fused/rerank 四路分数的命中列表（可视化数据源）。"""
    top_k = top_k or settings.rag_top_k
    pool = top_k * 3
    doc_filters = _doc_filters(symbol=symbol, doc_types=doc_types, trust_levels=trust_levels)

    # 查询改写+多路扩展（关闭/失败时 plan.queries == [query]，行为与改造前一致）
    plan = rewrite_and_expand(query)

    chunk_map: dict[int, DocumentChunk] = {}
    rrf_scores: dict[int, float] = {}
    keyword_component: dict[int, float] = {}
    vector_component: dict[int, float] = {}

    # 关键词召回：用改写后的检索句拆词（更干净的实体），逐词 LIKE
    keywords = _memory._extract_keywords(plan.rewritten)
    if keywords:
        keyword_rows = db.scalars(
            select(DocumentChunk)
            .join(Document, Document.id == DocumentChunk.doc_id)
            .where(
                DocumentChunk.status == "indexed",
                or_(*[DocumentChunk.content.like(f"%{kw}%") for kw in keywords[:5]]),
                *doc_filters,
            )
            .order_by(DocumentChunk.id.desc())
            .limit(pool)
        ).all()
        for rank, chunk in enumerate(keyword_rows):
            score = _memory.HYBRID_KEYWORD_WEIGHT / (_RRF_K + rank + 1)
            chunk_map[chunk.id] = chunk
            rrf_scores[chunk.id] = rrf_scores.get(chunk.id, 0.0) + score
            keyword_component[chunk.id] = score

    # 向量召回：多路（改写句+扩展句各嵌入一次），每路 store 出 (vector_id, 相似度)，
    # 按各自排名并入同一 RRF 池——多路命中同一 chunk 会累计得分（跨查询一致性=更强信号）。
    store = get_store()
    if store is not None:
        query_vecs = _memory._embed(plan.queries)  # _embed 接受 list，一次批量嵌入 N 条
        for query_vec in query_vecs:
            try:
                vector_hits = store.query(query_vec, pool)
            except Exception as exc:  # noqa: BLE001
                from backend.app.core.applog import log_event

                log_event("rag", "WARNING", f"向量召回失败（本次退化为纯关键词）: {str(exc)[:160]}")
                continue
            if not vector_hits:
                continue
            rows = db.scalars(
                select(DocumentChunk)
                .join(Document, Document.id == DocumentChunk.doc_id)
                .where(
                    DocumentChunk.vector_id.in_([vid for vid, _ in vector_hits]),
                    DocumentChunk.status == "indexed",
                    *doc_filters,
                )
            ).all()
            by_vector_id = {chunk.vector_id: chunk for chunk in rows}
            for rank, (vector_id, similarity) in enumerate(vector_hits):
                chunk = by_vector_id.get(vector_id)
                if chunk is None:  # 被过滤/已删/已禁用
                    continue
                chunk_map[chunk.id] = chunk
                rrf_scores[chunk.id] = rrf_scores.get(chunk.id, 0.0) + _memory.HYBRID_VECTOR_WEIGHT / (_RRF_K + rank + 1)
                # 同一 chunk 被多路命中时保留最高相似度（展示用；RRF 分已累计）
                vector_component[chunk.id] = max(vector_component.get(chunk.id, 0.0), similarity)

    if not chunk_map:
        return []

    fused_all = sorted(chunk_map.values(), key=lambda c: rrf_scores.get(c.id, 0.0), reverse=True)
    # 跨文档去重：同一篇资讯常被搜索缓存多次（不同日期抓同一研报），相同 content_hash 只保留
    # 融合分最高的一条，避免 top-K 被三条一模一样的内容占满
    seen_hashes: set[str] = set()
    fused: list[DocumentChunk] = []
    for chunk in fused_all:
        key = chunk.content_hash or f"id-{chunk.id}"
        if key in seen_hashes:
            continue
        seen_hashes.add(key)
        fused.append(chunk)
        if len(fused) >= pool:
            break

    # rerank 精排（复用记忆同款 API），失败/未配置回退融合排序
    scored = _memory._rerank_scores(query, [chunk.content for chunk in fused], top_k)
    if scored:
        final: list[tuple[DocumentChunk, float | None]] = [
            (fused[index], score) for index, score in scored if 0 <= index < len(fused)
        ][:top_k]
    else:
        final = [(chunk, None) for chunk in fused[:top_k]]

    return [_hit_out(chunk, rerank_score, vector_component, keyword_component, rrf_scores) for chunk, rerank_score in final]


def _doc_filters(*, symbol: str | None, doc_types: list[str] | None, trust_levels: list[str] | None) -> list:
    # 历史版本曾把个人 Agent 报告镜像到共享 documents。即便数据库尚未执行
    # 清理迁移，也不能让这些旧切块通过关键词或向量召回跨用户暴露。
    filters: list = [Document.doc_type != "report"]
    if doc_types:
        filters.append(Document.doc_type.in_(doc_types))
    if trust_levels:
        filters.append(Document.trust_level.in_(trust_levels))
    if symbol:
        filters.append(Document.id.in_(select(DocumentSymbol.doc_id).where(DocumentSymbol.symbol == symbol)))
    return filters


def _hit_out(
    chunk: DocumentChunk,
    rerank_score: float | None,
    vector_component: dict[int, float],
    keyword_component: dict[int, float],
    rrf_scores: dict[int, float],
) -> dict:
    doc = chunk.document
    return {
        "chunk_id": chunk.id,
        "chunk_index": chunk.chunk_index,
        "doc_id": chunk.doc_id,
        "title": doc.title if doc else "",
        "source": doc.source if doc else "",
        "doc_type": doc.doc_type if doc else "",
        "source_url": doc.source_url if doc else None,
        "content": chunk.content,
        "vector_score": round(vector_component.get(chunk.id, 0.0), 4),
        "keyword_score": round(keyword_component.get(chunk.id, 0.0), 6),
        "fused_score": round(rrf_scores.get(chunk.id, 0.0), 6),
        "rerank_score": round(rerank_score, 4) if rerank_score is not None else None,
    }
