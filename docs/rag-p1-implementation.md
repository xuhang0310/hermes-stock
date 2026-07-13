# RAG P1 实施方案（底座 + 知识库管理页）· 修订版

> 母方案：[rag-tech-design.md](rag-tech-design.md)（对标/原型/四期路径）
> 本文：P1 的执行级 spec。基于评审稿修订——修正 3 个问题（增量幂等、关键词召回、reindex 阻塞），落实 3 个提醒（权重 import、维度时序、任务默认值）
> 编码约束：**单一职责 / 奥卡姆剃刀 / DRY**（见文末逐条落实）

---

## 一、目标

把系统已沉淀的内部语料——`documents` 表 330 条 tavily 搜索缓存 + `agent_reports` 数字人报告——切块→嵌入→向量索引，变成可语义检索的知识库；交付管理页（统计/列表/筛选/重建索引）与基础检索 API。**P1 只吃内部纯文本**（无 pypdf/docx 依赖），上传解析留 P2。

## 二、复用清单（DRY 的落点，开工前定死）

| 直接 import（零复制） | 平行新建（明确不复用的理由） | 绝不动 |
| --- | --- | --- |
| `memory._embed(texts)`（批量嵌入，内部按 10 分块） | `_KnowledgeStore`——`_MilvusStore/_ChromaStore` schema 硬编码 `user_id`，RAG 需要 `doc_id/doc_type/…`，复用=魔改原类 | `memory.py` 的检索链路（生产已万级评测，P1 不背回归风险） |
| `memory._ensure_embedding()` + `_embedding_dim`（建 collection 前先初始化，解决维度时序） | `retrieval.py` 的 RRF 融合——`_hybrid_search` 硬耦合 `UserMemory` 表 | `user_memories` collection |
| `memory._extract_keywords(query)`（关键词拆词——修正②的关键：**不是整句 LIKE**） | `chunk_text`——记忆无切块概念，新写 | `run_sql` 沙箱等安全红线 |
| `memory._rerank` 同款 rerank API 调用（抽 `_rerank_scores` 供两处共用，见 §5） | | |
| `memory.HYBRID_VECTOR_WEIGHT / HYBRID_KEYWORD_WEIGHT`（**import 常量，不抄 0.55/0.45**） | | |
| 调度器 `DEFAULT_JOBS`/`_execute_job` 扩展点、前端 `list-table/cell-actions/page-section` | | |

## 三、评审修正（相对原评审稿的三处改动）

1. **增量幂等改按 doc_id 判断**。原稿"扫 `content_hash` 不在 `document_chunks`"混用了文档级/块级 hash（永不命中→每天重复摄取）。改为：`doc_id NOT IN (SELECT DISTINCT doc_id FROM document_chunks)`；块级 `content_hash` 只用于同文档内去重。
2. **关键词召回复用 `_extract_keywords` 拆词**。原稿整句 `LIKE %query%` 对中文自然问句必然零命中；memory 的 0.45 关键词权重有效正是因为先拆词（memory.py:451）。
3. **reindex 改后台执行**。330 篇≈3000 块≈300 次嵌入调用，同步等完会挂几分钟。`POST /api/knowledge/reindex` 改为 fire-and-forget（`asyncio.create_task(to_thread(...))`）+ 立即返回 202 语义，前端轮询 `/stats` 看进度。

## 四、数据模型（models.py）

**DocumentChunk**：`id` PK · `doc_id`(FK documents, index) · `chunk_index` · `content`(Text) · `token_count` · `vector_id`(String64, 向量库主键=`"{doc_id}-{chunk_index}"`) · `embed_model`(String80, 换模型重建依据) · `status`(String24: indexed/failed/disabled) · `hit_count`(Int default 0) · `content_hash`(String80, index) · `created_at`

**KnowledgeUpload**（P2 用，P1 先建表）：`id` · `filename` · `mime_type` · `size` · `doc_id`(FK) · `status`(parsing/chunking/indexed/failed) · `chunk_count` · `error` · `uploaded_by` · `uploaded_at`

## 五、后端模块（单一职责分工）

| 模块 | 唯一职责 |
| --- | --- |
| `ai/rag_ingest.py` | 摄取：报告镜像→切块→嵌入→写库/写向量。含 `_KnowledgeStore`（仅向量 IO：ensure/upsert/query/delete）、`chunk_text`（纯函数，中文标点感知 700/80）、`ingest_document(doc_id)`、`mirror_agent_reports()`、`ingest_incremental()`、`run_ingest(reindex=False)` |
| `ai/retrieval.py` | 检索：`retrieve_chunks(db, query, top_k, symbol?, doc_types?, trust_levels?)` → 向量(store)+关键词(拆词 LIKE)双召回 → RRF(k=60, import 权重) → rerank → 带四路分数的 dict 列表 |
| `ai_api.py` | 仅 HTTP 编排：5 个 `/api/knowledge/*` 端点，业务全部委托上面两个模块 |
| `rhythm_scheduler.py` | 仅调度接线：`rag_ingest` job（每日 16:30 交易日、默认启用、不推送）→ `run_ingest()` |

**事务纪律**（沿用调度器教训）：嵌入 HTTP 调用期间不持有数据库事务——每篇文档"短会话读文本 → 无会话嵌入 → 短会话写块+upsert"。

**`_rerank_scores` 抽取**（DRY，对 memory.py 的唯一改动、行为不变）：把 `_rerank` 内部"调 /rerank API 拿 [(index, score)]"抽成 `_rerank_scores(query, texts, top_n)`，`_rerank` 改为调它后重排对象；`retrieval.py` 复用它拿到 `rerank_score`（原 `_rerank` 只返回排序丢分数，可视化需要分数）。

**Occam 取舍**：向量库 schema 按母方案存 `doc_id/doc_type/symbol/trust_level/published_at`（避免 P3/P4 重建），但 P1 的 `query()` **不做 store 级过滤**——过滤在 SQL JOIN 里做（一种过滤语言，不给 chroma/milvus 各写一套 expr）。

## 六、API（挂 ai_api.py router）

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/knowledge/documents?doc_type=&trust_level=&keyword=&page=` | system:view | 列表+切块统计（LEFT JOIN 聚合） |
| GET | `/api/knowledge/stats` | system:view | `{documents, chunks, indexed, failed, indexed_pct, backend, dim}` |
| GET | `/api/knowledge/documents/{id}/chunks` | system:view | 切块列表（content 截 300 字 + status + hit_count） |
| POST | `/api/knowledge/reindex` `{doc_id?}` | model:manage | 后台任务（修正③），全量=清 chunks 重灌，单文档=重灌该篇 |
| POST | `/api/knowledge/search` `{query, top_k?, symbol?}` | system:view | retrieve_chunks 直出（P1 基础版，P3 扩四列） |

## 七、前端

- `KnowledgeBaseView.vue`（新）：统计条 4 格 + 筛选 chips（doc_type/trust_level）+ 关键词搜索 + 列表（标题/类型/来源/可信度/切块数/状态/操作:查看切块）+ 切块行内展开 + 重建索引按钮（点击后按钮转"索引中"，轮询 stats 到数字稳定）。复用 `list-table/cell-actions/page-section` 全局样式（RhythmView 范例）。
- `App.vue` PageKey + 视图分支、`AppShell.vue` navItems 加「知识库」（system:view 显隐）。
- `types.ts`：`KnowledgeDocument / KnowledgeChunk / KnowledgeStats / KnowledgeSearchHit`；`api.ts` 对应 5 个方法。

## 八、settings / .env

`STOCK_RAG_ENABLED`(true) · `STOCK_RAG_MILVUS_COLLECTION`(knowledge_chunks) · `STOCK_RAG_TOP_K`(6) · `STOCK_RAG_CHUNK_SIZE`(700) · `STOCK_RAG_OVERLAP`(80) · `STOCK_RAG_MIN_SCORE`(0.35，P2 注入阈值先建)。向量后端/URI 复用 `STOCK_VECTOR_BACKEND`/`STOCK_MILVUS_URI`。

## 九、调度任务

`rag-ingest-1630`：daily 16:30、交易日、`job_kind=rag_ingest`、**默认启用**（规则类不经 LLM；首次全量≈300 次嵌入调用为一次性成本，日增量个位数篇）、不推送、失败只落 system_logs（零噪音纪律）。

## 十、验证清单

1. `python -m pytest tests/test_backend_api.py` 回归绿。
2. 启动后 `document_chunks`/`knowledge_uploads` 建表、`rag-ingest-1630` job 注册。
3. `POST /api/knowledge/reindex` → 轮询 stats：chunks>0、indexed_pct→100%、Milvus `knowledge_chunks` 有向量。
4. `POST /api/knowledge/search {"query":"宁德时代"}` → 命中带 vector/keyword/fused/rerank 四分数。
5. **幂等验证**：连跑两次 `run_ingest()`，第二次新增 0（修正①的验收）。
6. 前端 `vue-tsc` + 构建绿；知识库页统计/列表/筛选/切块/重建可用。

## 十一、三原则逐条落实

- **单一职责**：ingest 只管入库、retrieval 只管查询、store 只管向量 IO、API 层只做编排、调度只做接线——五个关注点五个位置。
- **奥卡姆**：不做切块器插件/解析器注册表（P1 只有一种纯文本切块）；store.query 不带过滤参数（SQL 一处过滤）；不建投机配置项；KnowledgeUpload 只建表不建端点（P2 的实体 P1 不长逻辑）。
- **DRY**：嵌入/拆词/rerank/权重全部 import memory 现有实现（含抽 `_rerank_scores` 消除"排序逻辑复制"）；vector_id 生成、hash 计算各一处；前端零新样式类。
