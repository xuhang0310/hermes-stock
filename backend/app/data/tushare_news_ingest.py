"""TuShare 新闻定时入库：拉 news/major_news/cctv_news → documents(doc_type=news) → 触发 RAG 索引。

单一职责：本模块只负责"把 TuShare 三类新闻原文落进知识库"。切块/嵌入/向量写在
ai/rag_ingest.py（ingest_document 按 doc_id 幂等），调度接线在 scheduler/rhythm_scheduler.py。

复用（DRY）：
- 数据源走 market.stock_tools._tushare_request（内部经 _data_source_api_key 解析 token，
  生产 token 存于数据源配置而非 .env，绝不能另写 httpx）。
- 去重走 documents.content_hash（已建索引；范式同 rag_ingest.mirror_agent_reports）。
- 嵌入走 rag_ingest.ingest_document；每日 rag-ingest 任务是无切块文档的兜底。

时区：TuShare 新闻时间均为北京时间，窗口/日期一律按 Asia/Shanghai 转 naive，与全库
naive datetime 惯例及 rhythm_scheduler.now_sh 一致（服务器进程时区无关）。

事务纪律：TuShare HTTP 期间不持有数据库事务（短会话读 hash → 无会话拉取 → 短会话写）。
容错：逐源/逐日 try/except，单源限流或无权限不影响其它源；整体绝不抛给调度器。
"""
from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from backend.app.core.applog import log_event
from backend.app.core.database import SessionLocal
from backend.app.core.models import Document
from backend.app.core.settings import settings

_SHANGHAI = ZoneInfo("Asia/Shanghai")

# news 接口逐源轮询（src 必填）；每源独立容错，清单在此集中维护
NEWS_SOURCES = ["sina", "wallstreetcn", "10jqka", "eastmoney", "yuncaijing"]

# 各接口显式字段串（空串虽可，但显式更稳）
_FIELDS = {
    "news": "datetime,content,title,channels",
    "major_news": "title,content,url,src",
    "cctv_news": "date,title,content",
}

# job_key → 拉取规格（执行器按 job_key 取；窗口 > 周期，漏跑一轮也不丢，重复靠 hash 去重）
NEWS_INGEST_SPECS: dict[str, dict] = {
    "news-ingest-quick-15m": {"label": "快讯入库", "api": "news", "window_minutes": 40},
    "news-ingest-major-1h": {"label": "要闻入库", "api": "major_news", "window_minutes": 120},
    "news-ingest-cctv-daily": {"label": "联播入库", "api": "cctv_news", "days_back": 1},
}

_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _now_sh() -> datetime:
    return datetime.now(_SHANGHAI).replace(tzinfo=None)


def _strip_html(text: str) -> str:
    """major_news 正文是 HTML：剥标签 + 反转义 + 折叠空白，避免标签污染向量与展示。"""
    if not text:
        return ""
    cleaned = _HTML_TAG.sub(" ", text)
    cleaned = html.unescape(cleaned)
    return _WS.sub(" ", cleaned).strip()


def _parse_dt(value: object) -> datetime | None:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _parse_date(value: object) -> datetime | None:
    text = str(value or "").strip()
    try:
        return datetime.strptime(text, "%Y%m%d")
    except ValueError:
        return _parse_dt(text)


def _to_document_fields(api: str, row: dict, src: str, now: datetime) -> dict | None:
    """把一条 TuShare 行归一化为 documents 字段；无正文无标题则返回 None 丢弃。"""
    raw_title = str(row.get("title") or "").strip()
    content = str(row.get("content") or "")
    if api == "major_news":
        content = _strip_html(content)
    content = content.strip()
    if not content and (not raw_title or raw_title.lower() == "none"):
        return None

    title = raw_title
    if not title or title.lower() == "none":  # news 的 title 常是字符串 "None"，正文才是真内容
        title = content[:80]
    title = (title or "").strip()[:500] or content[:80]

    if api == "news":
        source = f"tushare:news:{src}"
    elif api == "major_news":
        source = f"tushare:major_news:{str(row.get('src') or '').strip()}"
    else:
        source = f"tushare:{api}"

    source_url: str | None = None
    if api == "major_news":
        source_url = str(row.get("url") or "").strip() or None

    if api == "cctv_news":
        published_at = _parse_date(row.get("date")) or now
    else:
        published_at = _parse_dt(row.get("datetime")) or now

    return {
        "title": title,
        "content": content,
        "source": source[:120],
        "source_url": source_url,
        "published_at": published_at,
    }


def _content_hash(api: str, src: str, published_at: datetime | None, title: str, content: str) -> str:
    """稳定去重键（documents.content_hash）。title 常为 None，用正文前缀防碰撞。"""
    pub_key = published_at.strftime("%Y-%m-%d %H:%M:%S") if published_at else ""
    material = f"tushare:{api}:{src}:{pub_key}:{(title or '')[:80]}:{(content or '')[:120]}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:64]


def _fetch_rows(api: str, spec: dict, now: datetime):
    """按源/按日拉取，yield (src, rows)；单源失败记日志跳过，绝不抛。"""
    from backend.app.market.stock_tools import _tushare_request

    fields = _FIELDS[api]
    if api == "cctv_news":
        days_back = int(spec.get("days_back", 1))
        for offset in range(days_back + 1):  # 今天 + 往前 days_back 天（联播晚间发布，兜住时序）
            day = (now.date() - timedelta(days=offset)).strftime("%Y%m%d")
            try:
                result = _tushare_request(api, {"date": day}, fields)
            except Exception as exc:  # noqa: BLE001
                log_event("news_ingest", "WARNING", f"cctv_news {day} 拉取异常: {str(exc)[:200]}")
                continue
            if "error" in result:
                log_event("news_ingest", "WARNING", f"cctv_news {day} 拉取失败: {str(result.get('error'))[:200]}")
                continue
            yield "", result.get("rows", []) or []
        return

    window = int(spec.get("window_minutes", 60))
    start = (now - timedelta(minutes=window)).strftime("%Y-%m-%d %H:%M:%S")
    end = now.strftime("%Y-%m-%d %H:%M:%S")
    sources = NEWS_SOURCES if api == "news" else [None]
    for src in sources:
        params: dict = {"start_date": start, "end_date": end}
        if src:
            params["src"] = src
        label = src or "-"
        try:
            result = _tushare_request(api, params, fields)
        except Exception as exc:  # noqa: BLE001
            log_event("news_ingest", "WARNING", f"{api} src={label} 拉取异常: {str(exc)[:200]}")
            continue
        if "error" in result:
            log_event("news_ingest", "WARNING", f"{api} src={label} 拉取失败: {str(result.get('error'))[:200]}")
            continue
        yield (src or ""), result.get("rows", []) or []


def run_news_ingest(job_key: str) -> str:
    """拉取并入库一个新闻任务，返回人读统计（绝不抛；失败落日志由健康检查汇总）。"""
    spec = NEWS_INGEST_SPECS.get(job_key)
    if spec is None:
        return f"未知的新闻入库任务: {job_key}"
    api = spec["api"]
    label = spec.get("label", api)
    now = _now_sh()

    fetched = new = indexed = skipped = 0
    try:
        # 短会话一：读现有 hash 集合（去重基准）
        with SessionLocal() as db:
            existing = {
                h
                for h in db.scalars(select(Document.content_hash).where(Document.doc_type == "news"))
                if h
            }

        # 无会话阶段：拉取 + 归一化 + 判重
        pending: list[tuple[dict, str]] = []
        for src, rows in _fetch_rows(api, spec, now):
            fetched += len(rows)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                fields = _to_document_fields(api, row, src, now)
                if fields is None:
                    continue
                digest = _content_hash(api, src, fields["published_at"], fields["title"], fields["content"])
                if digest in existing:
                    skipped += 1
                    continue
                existing.add(digest)
                pending.append((fields, digest))

        # 短会话二：批量写入新文档
        new_ids: list[int] = []
        if pending:
            with SessionLocal() as db:
                for fields, digest in pending:
                    doc = Document(
                        doc_type="news",
                        title=fields["title"],
                        content=fields["content"],
                        source=fields["source"],
                        source_url=fields["source_url"],
                        published_at=fields["published_at"],
                        trust_level="normal",
                        is_external_untrusted=True,
                        content_hash=digest,
                    )
                    db.add(doc)
                    db.flush()
                    new_ids.append(doc.id)
                db.commit()
            new = len(new_ids)

        # 无会话阶段：best-effort 逐篇索引（嵌入 HTTP 不持事务；失败由每日 rag-ingest 兜底）
        if new_ids and settings.rag_enabled:
            from backend.app.ai.rag_ingest import ingest_document

            for doc_id in new_ids:
                try:
                    ok, _failed = ingest_document(doc_id)
                    if ok:
                        indexed += 1
                except Exception as exc:  # noqa: BLE001
                    log_event("news_ingest", "WARNING", f"文档 {doc_id} 索引失败（每日任务兜底）: {str(exc)[:200]}")
    except Exception as exc:  # noqa: BLE001
        log_event("news_ingest", "ERROR", f"{label}异常: {str(exc)[:300]}")
        return f"{label}异常: {str(exc)[:200]}"

    summary = f"{label}：拉取 {fetched} 条，新增 {new} 篇"
    summary += f"，索引 {indexed} 篇" if settings.rag_enabled else "（RAG 未开启，待手动/每日索引）"
    summary += f"，跳过重复 {skipped}"
    log_event("news_ingest", "INFO", summary, to_db=False)
    return summary
