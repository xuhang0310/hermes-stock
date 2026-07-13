"""RAG 知识库摄取管线（P1）：内部语料（documents + agent_reports）→ 切块 → 嵌入 → 向量索引。

单一职责：本模块只负责"入库"（镜像报告 / 切块 / 嵌入 / 写 document_chunks / 写向量库）；
检索在 ai/retrieval.py，HTTP 编排在 ai/ai_api.py，调度接线在 scheduler/rhythm_scheduler.py。

复用（DRY）：嵌入走 memory 的 _embed/_ensure_embedding/_embedding_dim（与用户记忆同一嵌入
空间、同一后台数据源配置）；向量后端沿用 settings.vector_backend/milvus_uri，但 collection
独立（knowledge_chunks，schema 带 doc_id 等业务字段），与 user_memories 互不相扰。
换嵌入模型 = drop 本 collection 重建（记忆模块同款教训）。

事务纪律：嵌入 HTTP 调用期间不持有数据库事务——每篇文档"短会话读 → 无会话嵌入 →
短会话写"（调度器价格同步同款教训）。

幂等：按 doc_id 判断（已有切块的文档跳过）；块级 content_hash 只做同文档内去重。
摄取失败不写行、只记日志，下轮增量自动重试（自愈优于簿记）。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from sqlalchemy import func, select, text

from backend.app.ai import memory as _memory
from backend.app.core.applog import log_event
from backend.app.core.database import SessionLocal
from backend.app.core.models import AgentReport, Document, DocumentChunk, DocumentSymbol
from backend.app.core.settings import settings

_CHROMA_PATH = Path(__file__).resolve().parents[2] / "chroma_data"  # 与 memory 同库不同 collection
_MIN_CHUNK_CHARS = 20  # 过短碎块直接丢弃
_MIN_DOC_CHARS = 30  # 正文短于此的文档不入库


# --------------------------------------------------------------------------- 切块（纯函数）
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；\n])")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")          # ![Image 13](https://...)
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")           # [文字](url) → 文字
_BARE_URL = re.compile(r"https?://\S+")                    # 裸 URL（含超长 token 查询串）
_MD_NOISE = re.compile(r"[*#`_]{1,}")                      # markdown 强调/标题符号
_SPACES = re.compile(r"[ \t　]{2,}")


def _clean_text(text: str) -> str:
    """摄取前清洗：剥 markdown 图片/链接、裸 URL 与强调符号——URL 垃圾会污染向量也毁展示。"""
    cleaned = _MD_IMAGE.sub(" ", text or "")
    cleaned = _MD_LINK.sub(r"\1", cleaned)
    cleaned = _BARE_URL.sub(" ", cleaned)
    cleaned = _MD_NOISE.sub("", cleaned)
    cleaned = _SPACES.sub(" ", cleaned)
    return cleaned.strip()


def chunk_text(text: str, size: int | None = None, overlap: int | None = None) -> list[str]:
    """中文标点感知切块：按句累积到 size 封块，块间保留 overlap 字尾部重叠；超长无标点段落硬切。"""
    size = size or settings.rag_chunk_size
    overlap = settings.rag_chunk_overlap if overlap is None else overlap
    chunks: list[str] = []
    buffer = ""
    for sentence in (s for s in _SENTENCE_SPLIT.split(text or "") if s.strip()):
        if len(sentence) > size:
            if buffer.strip():
                chunks.append(buffer.strip())
                buffer = ""
            chunks.extend(sentence[i:i + size] for i in range(0, len(sentence), size))
            continue
        if buffer and len(buffer) + len(sentence) > size:
            chunks.append(buffer.strip())
            buffer = buffer[-overlap:] if overlap else ""
        buffer += sentence
    if buffer.strip():
        chunks.append(buffer.strip())
    return [c for c in chunks if len(c) >= _MIN_CHUNK_CHARS]


# --------------------------------------------------------------------------- 向量 IO
class _KnowledgeStore:
    """知识库向量 IO（唯一职责：upsert/query/delete）。

    照搬 memory 双后端建库模式、schema 换成知识库字段。P1 不做 store 级过滤——
    过滤统一在 retrieval 的 SQL JOIN 里做（一种过滤语言，奥卡姆）；schema 仍存
    doc_id/doc_type 等字段，为 P3/P4 免重建。
    """

    def __init__(self) -> None:
        if _memory._ensure_embedding() is None or not _memory._embedding_dim:
            raise RuntimeError("嵌入服务不可用，无法初始化知识库向量库")
        self._dim = _memory._embedding_dim
        self._backend = (settings.vector_backend or "chroma").lower()
        self._name = settings.rag_milvus_collection
        if self._backend == "milvus":
            from pymilvus import DataType, MilvusClient

            self._c = MilvusClient(uri=settings.milvus_uri, token=settings.milvus_token or "")
            if not self._c.has_collection(self._name):
                schema = self._c.create_schema(auto_id=False, enable_dynamic_field=True)
                schema.add_field("vector_id", DataType.VARCHAR, is_primary=True, max_length=64)
                schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self._dim)
                schema.add_field("doc_id", DataType.INT64)
                schema.add_field("doc_type", DataType.VARCHAR, max_length=32)
                schema.add_field("symbol", DataType.VARCHAR, max_length=24)
                schema.add_field("trust_level", DataType.VARCHAR, max_length=24)
                schema.add_field("published_at", DataType.INT64)
                index_params = self._c.prepare_index_params()
                index_params.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
                self._c.create_collection(collection_name=self._name, schema=schema, index_params=index_params)
            self._c.load_collection(self._name)
        else:
            import chromadb

            _CHROMA_PATH.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(path=str(_CHROMA_PATH))
            self._col = client.get_or_create_collection(name=self._name, metadata={"hnsw:space": "cosine"})

    def upsert(self, ids: list[str], vectors: list[list[float]], metadatas: list[dict]) -> None:
        if self._backend == "milvus":
            self._c.upsert(
                collection_name=self._name,
                data=[
                    {
                        "vector_id": vid,
                        "vector": vec,
                        "doc_id": int(meta.get("doc_id", 0)),
                        "doc_type": str(meta.get("doc_type") or ""),
                        "symbol": str(meta.get("symbol") or ""),
                        "trust_level": str(meta.get("trust_level") or ""),
                        "published_at": int(meta.get("published_at", 0)),
                    }
                    for vid, vec, meta in zip(ids, vectors, metadatas)
                ],
            )
        else:
            self._col.upsert(ids=list(ids), embeddings=list(vectors), metadatas=list(metadatas))

    def query(self, vector: list[float], n: int) -> list[tuple[str, float]]:
        """返回 [(vector_id, 相似度)]，分越高越相关；两后端分数口径与 memory 一致。"""
        if self._backend == "milvus":
            res = self._c.search(
                collection_name=self._name, data=[vector], limit=n,
                output_fields=[], consistency_level="Strong",  # 刚索引的块需立即可检索
            )
            # 注意：MilvusClient 命中里主键的键名 = 主键字段名（本集合是 vector_id）；
            # memory 的集合主键恰好叫 id 才能用 hit["id"]，这里两个键都兜住
            return [
                (vid, float(hit.get("distance", 0.0)))
                for hit in (res[0] if res else [])
                if (vid := str(hit.get("vector_id") or hit.get("id") or ""))
            ]
        res = self._col.query(query_embeddings=[vector], n_results=n, include=["distances"])
        ids = res.get("ids", [[]])[0]
        dists = res.get("distances", [[]])[0]
        return [(str(i), max(0.0, 1.0 - float(d) / 2.0)) for i, d in zip(ids, dists)]

    def delete(self, ids: list[str]) -> None:
        if not ids:
            return
        if self._backend == "milvus":
            self._c.delete(collection_name=self._name, ids=list(ids))
        else:
            self._col.delete(ids=list(ids))


_store: _KnowledgeStore | None = None
_store_failed_at: float = 0.0
_STORE_RETRY_SECONDS = 60  # 初始化失败不永久缓存：冷却后允许重试（Milvus/嵌入服务临时抖动可自愈）


def get_store() -> _KnowledgeStore | None:
    """惰性单例；嵌入/向量库不可用时返回 None（摄取与检索优雅降级，不影响主功能）。"""
    global _store, _store_failed_at
    if _store is not None:
        return _store
    import time as _time

    if _store_failed_at and _time.monotonic() - _store_failed_at < _STORE_RETRY_SECONDS:
        return None
    try:
        _store = _KnowledgeStore()
        _store_failed_at = 0.0
        log_event("rag", "INFO", f"知识库向量库就绪：{settings.vector_backend}/{settings.rag_milvus_collection}", to_db=False)
    except Exception as exc:  # noqa: BLE001
        log_event("rag", "ERROR", f"知识库向量库初始化失败（{_STORE_RETRY_SECONDS}s 后可重试）: {str(exc)[:160]}")
        _store_failed_at = _time.monotonic()
    return _store


# --------------------------------------------------------------------------- 摄取
def _report_hash(report_id: str) -> str:
    """报告镜像文档的稳定唯一键（documents.content_hash）。"""
    return hashlib.sha256(f"agent_report:{report_id}".encode()).hexdigest()[:64]


def mirror_agent_reports() -> int:
    """把尚未镜像的 agent_reports 转成 documents 行（doc_type=report），统一走文档摄取。"""
    with SessionLocal() as db:
        existing = {
            h for h in db.scalars(select(Document.content_hash).where(Document.doc_type == "report")) if h
        }
        created = 0
        for report in db.scalars(select(AgentReport)).all():
            digest = _report_hash(report.report_id)
            if digest in existing or not (report.content or "").strip():
                continue
            db.add(
                Document(
                    doc_type="report",
                    title=report.title,
                    content=report.content,
                    summary=report.conclusion,
                    source=f"agent:{report.agent_name}",
                    published_at=report.created_at,
                    trust_level="trusted",
                    is_external_untrusted=False,
                    content_hash=digest,
                )
            )
            created += 1
        db.commit()
    return created


def ingest_document(doc_id: int, *, force: bool = False) -> tuple[int, int]:
    """切块+嵌入+索引一篇文档，返回 (成功块数, 失败块数)。已有切块且非 force 时跳过。"""
    with SessionLocal() as db:
        doc = db.get(Document, doc_id)
        if doc is None:
            return 0, 0
        if not force:
            existing = db.scalar(
                select(func.count()).select_from(DocumentChunk).where(DocumentChunk.doc_id == doc_id)
            )
            if existing:
                return 0, 0
        text = _clean_text(doc.content or doc.summary or "")
        title = (doc.title or "").strip()
        if title and title not in text:
            text = f"{title}。{text}"  # 标题并入正文提升召回（tavily 缓存正文常为摘要片段）
        meta = {
            "doc_id": doc.id,
            "doc_type": doc.doc_type,
            "trust_level": doc.trust_level,
            "published_at": int(doc.published_at.timestamp()) if doc.published_at else 0,
            "symbol": db.scalar(select(DocumentSymbol.symbol).where(DocumentSymbol.doc_id == doc_id).limit(1)) or "",
        }

    if len(text) < _MIN_DOC_CHARS:
        return 0, 0
    pieces = chunk_text(text)
    if not pieces:
        return 0, 0

    vectors = _memory._embed(pieces)  # HTTP 调用，此刻不持有任何数据库事务
    store = get_store()
    if vectors is None or store is None:
        return 0, len(pieces)  # 不写行保持可重试（下轮增量自动再来）

    _, _, embed_model = _memory._resolve_embedding_cfg()
    rows: list[DocumentChunk] = []
    ids: list[str] = []
    vecs: list[list[float]] = []
    seen_hashes: set[str] = set()
    for index, (piece, vector) in enumerate(zip(pieces, vectors)):
        digest = hashlib.sha256(piece.encode()).hexdigest()[:64]
        if digest in seen_hashes:  # 同文档内重复块去重
            continue
        seen_hashes.add(digest)
        vector_id = f"{doc_id}-{index}"
        rows.append(
            DocumentChunk(
                doc_id=doc_id, chunk_index=index, content=piece, token_count=len(piece),
                vector_id=vector_id, embed_model=embed_model or None, content_hash=digest,
            )
        )
        ids.append(vector_id)
        vecs.append(vector)

    with SessionLocal() as db:
        try:
            old_rows: list[DocumentChunk] = []
            if force:
                old_rows = db.scalars(select(DocumentChunk).where(DocumentChunk.doc_id == doc_id)).all()
                for row in old_rows:
                    db.delete(row)
                db.flush()
            store.upsert(ids, vecs, [dict(meta) for _ in ids])
            db.add_all(rows)
            # force 重灌：upsert 成功后再删残留向量（块数减少时旧 vector_id 不在新 ids 的），
            # 避免"先 delete 不可逆"——upsert 失败时旧向量还在，可再 reindex
            if force:
                new_ids = set(ids)
                residual = [row.vector_id for row in old_rows if row.vector_id not in new_ids]
                if residual:
                    store.delete(residual)
            db.commit()
            return len(rows), 0
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            log_event("rag", "ERROR", f"文档 {doc_id} 索引失败: {str(exc)[:200]}")
            # 补偿：清该 doc 的 chunks 行（独立短会话），下轮增量按 doc_id 幂等会整篇重灌，自愈闭环
            try:
                with SessionLocal() as cleanup:
                    cleanup.execute(text("DELETE FROM document_chunks WHERE doc_id = :d"), {"d": doc_id})
                    cleanup.commit()
            except Exception:  # noqa: BLE001
                pass
            return 0, len(rows)


# 摄取进度（单一来源：调度任务与 reindex 后台任务都写这里，stats 接口原样透出）。
# 单写者（同一时刻只有一个摄取线程，见 run_ingest 防重），读者只读快照，无需加锁。
_progress: dict = {
    "running": False,
    "processed": 0,
    "total": 0,
    "indexed": 0,
    "failed": 0,
    "last_result": "",
}


def get_progress() -> dict:
    """摄取进度快照（前端进度条/完成判定的数据源）。"""
    return dict(_progress)


def ingest_incremental(*, force: bool = False) -> dict:
    """镜像新报告 + 摄取所有尚无切块的文档（doc_id 幂等）。force=True 全量重灌。"""
    mirrored = mirror_agent_reports()
    with SessionLocal() as db:
        query = select(Document.id).order_by(Document.id)
        if not force:
            query = query.where(Document.id.not_in(select(DocumentChunk.doc_id).distinct()))
        candidates = list(db.scalars(query))
    _progress.update(processed=0, total=len(candidates), indexed=0, failed=0)
    indexed_docs = indexed_chunks = failed_chunks = 0
    embed_failure_logged = False
    for doc_id in candidates:
        ok, failed = ingest_document(doc_id, force=force)
        if ok:
            indexed_docs += 1
            indexed_chunks += ok
        if failed and not ok and not embed_failure_logged:
            # 嵌入服务不可用时每篇都会失败——只落一条日志说明原因，避免刷屏（每篇写库失败已各自留痕）
            log_event("rag", "WARNING", f"文档 {doc_id} 嵌入失败（嵌入服务不可用或向量库未就绪），本轮同类失败不再重复记录")
            embed_failure_logged = True
        failed_chunks += failed
        _progress.update(processed=_progress["processed"] + 1, indexed=indexed_docs, failed=failed_chunks)
    return {
        "mirrored_reports": mirrored,
        "candidates": len(candidates),
        "docs": indexed_docs,
        "chunks": indexed_chunks,
        "failed": failed_chunks,
    }


def run_ingest(*, force: bool = False) -> str:
    """摄取入口（调度任务与 reindex 后台任务共用），返回人读统计。自带防重（真源头互斥）。"""
    if not settings.rag_enabled:
        return "RAG 已关闭（STOCK_RAG_ENABLED=false），跳过"
    if _progress["running"]:
        return "已有摄取在跑，跳过本次触发"
    _progress["running"] = True
    try:
        stats = ingest_incremental(force=force)
        preview = (
            f"镜像报告 {stats['mirrored_reports']} 篇；候选 {stats['candidates']} 篇，"
            f"新索引 {stats['docs']} 篇 / {stats['chunks']} 块"
        )
        if stats["failed"]:
            preview += f"；失败 {stats['failed']} 块（下轮增量自动重试）"
    except Exception as exc:  # noqa: BLE001
        preview = f"摄取异常中断: {str(exc)[:200]}"
        log_event("rag", "ERROR", preview)
    finally:
        _progress["running"] = False
        _progress["last_result"] = preview
    return preview


def disable_document(doc_id: int) -> dict:
    """文档级禁用：全部切块置 disabled + 从向量库摘除（可用「重灌」恢复，软删口径）。"""
    with SessionLocal() as db:
        rows = db.scalars(select(DocumentChunk).where(DocumentChunk.doc_id == doc_id)).all()
        if not rows:
            return {"error": "该文档还没有切块，无需禁用"}
        store = get_store()
        if store is not None:
            try:
                store.delete([row.vector_id for row in rows])
            except Exception as exc:  # noqa: BLE001
                return {"error": f"删除向量失败: {str(exc)[:160]}"}
        for row in rows:
            row.status = "disabled"
        db.commit()
        return {"ok": True, "disabled_chunks": len(rows)}
