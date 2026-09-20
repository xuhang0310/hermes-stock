<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api'
import type { KnowledgeStats, KnowledgeDocument, KnowledgeChunk, KnowledgeSearchHit } from '../types'
import { beijingFromDb } from '../utils/datetime'

const stats = ref<KnowledgeStats | null>(null)
const documents = ref<KnowledgeDocument[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filterDocType = ref('')
const filterTrust = ref('')
const keyword = ref('')
const loading = ref(false)
const error = ref('')
const chunks = ref<Record<number, KnowledgeChunk[]>>({})
const loadingChunks = ref<number | null>(null)
const reindexing = ref(false)
const reindexMsg = ref('')
const activeTab = ref<'docs' | 'search'>('docs')
const searchQuery = ref('')
const searchTopK = ref(6)
const searchHits = ref<KnowledgeSearchHit[]>([])
const searched = ref(false)
const searching = ref(false)
let pollTimer: ReturnType<typeof setInterval> | null = null

// 值 → 中文徽章（列表里不出现裸英文 news/tavily/normal）
const TYPE_LABELS: Record<string, string> = { news: '新闻', report: '内部报告', notice: '公告', research: '研报', upload: '上传' }
const TRUST_META: Record<string, { label: string; cls: string }> = {
  trusted: { label: '可信', cls: 'pill-blue' },
  normal: { label: '外部', cls: 'pill-gray' },
  untrusted: { label: '未核实', cls: 'pill-red' }
}
const STATUS_META: Record<string, { label: string; cls: string }> = {
  indexed: { label: '✓ 已索引', cls: 'pill-green' },
  disabled: { label: '已禁用', cls: 'pill-red' },
  pending: { label: '未索引', cls: 'pill-gray' }
}

const TITLE_DISPLAY_LIMIT = 80

function displayTitle(title: string) {
  return title.length > TITLE_DISPLAY_LIMIT ? `${title.slice(0, TITLE_DISPLAY_LIMIT)}...` : title
}

function typeLabel(t: string) {
  return TYPE_LABELS[t] || t
}
function trustMeta(t: string) {
  return TRUST_META[t] || { label: t, cls: 'pill-gray' }
}
function statusMeta(s: string) {
  return STATUS_META[s] || { label: s, cls: 'pill-gray' }
}

const typeChips = computed(() => {
  const byType = stats.value?.by_type || {}
  const entries = Object.entries(byType).sort((a, b) => b[1] - a[1])
  return [
    { key: '', label: '全部', count: stats.value?.documents ?? 0 },
    ...entries.map(([key, count]) => ({ key, label: typeLabel(key), count }))
  ]
})

const ingestPct = computed(() => {
  const ing = stats.value?.ingest
  if (!ing || !ing.total) return 0
  return Math.round((ing.processed / ing.total) * 100)
})

async function loadStats() {
  try {
    stats.value = await api.knowledgeStats()
  } catch {
    // 统计非阻塞
  }
}

async function loadDocuments() {
  loading.value = true
  error.value = ''
  try {
    const res = await api.knowledgeDocuments({
      doc_type: filterDocType.value || undefined,
      trust_level: filterTrust.value || undefined,
      keyword: keyword.value || undefined,
      page: page.value,
      page_size: pageSize.value
    })
    documents.value = res.items
    total.value = res.total
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

function pickType(key: string) {
  if (filterDocType.value === key) return
  filterDocType.value = key
  page.value = 1
  loadDocuments()
}

async function loadChunks(d: KnowledgeDocument) {
  if (chunks.value[d.id]) {
    delete chunks.value[d.id]
    return
  }
  loadingChunks.value = d.id
  try {
    chunks.value[d.id] = await api.knowledgeChunks(d.id)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载切块失败'
  } finally {
    loadingChunks.value = null
  }
}

async function disableDoc(d: KnowledgeDocument) {
  if (!window.confirm(`禁用《${d.title}》？其切块将从检索中摘除（可用「重灌」恢复）`)) return
  try {
    await api.knowledgeDisable(d.id)
    delete chunks.value[d.id]
    await Promise.all([loadDocuments(), loadStats()])
  } catch (err) {
    error.value = err instanceof Error ? err.message : '禁用失败'
  }
}

function stopReindex(msg: string) {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
  reindexing.value = false
  reindexMsg.value = msg
  loadStats()
  loadDocuments()
}

async function reindex(docId?: number) {
  reindexing.value = true
  reindexMsg.value = ''
  try {
    await api.knowledgeReindex(docId)
    reindexMsg.value = docId ? `已启动重灌文档 #${docId}` : '已启动全量重建'
    // 完成判定以后端摄取进度 ingest.running 为准（单篇重灌秒级完成，不产生 running，两轮后收尾）
    let sawRunning = false
    let ticks = 0
    pollTimer = setInterval(async () => {
      ticks += 1
      await loadStats()
      const ing = stats.value?.ingest
      if (ing?.running) {
        sawRunning = true
        return
      }
      if (sawRunning || ticks >= 2) {
        stopReindex(ing?.last_result ? `完成：${ing.last_result}` : '重建完成')
      }
    }, 3000)
    setTimeout(() => {
      if (pollTimer) stopReindex('轮询超时（15min），请到运行日志查看摄取结果')
    }, 900000)
  } catch (err) {
    reindexMsg.value = err instanceof Error ? err.message : '重建失败'
    reindexing.value = false
  }
  if (docId && chunks.value[docId]) {
    delete chunks.value[docId]
  }
}

async function runSearch() {
  if (!searchQuery.value.trim()) return
  searching.value = true
  try {
    const res = await api.knowledgeSearch(searchQuery.value, searchTopK.value || 6)
    searchHits.value = res.hits
    searched.value = true
  } catch (err) {
    error.value = err instanceof Error ? err.message : '检索失败'
  } finally {
    searching.value = false
  }
}

// 分数条宽度：同一指标相对本次结果的最大值归一（RRF 分数量级很小，绝对值没法直接当百分比）
function barWidth(h: KnowledgeSearchHit, key: 'vector_score' | 'keyword_score' | 'fused_score' | 'rerank_score') {
  const value = (h[key] ?? 0) as number
  const max = Math.max(...searchHits.value.map((x) => (x[key] ?? 0) as number), 0.0001)
  return Math.round((value / max) * 100)
}

// 0 分=该路没召回到，显示 — 而不是 0（向量 0 曾掩盖过向量召回整条腿故障）
function fmtScore(value: number | null | undefined) {
  return value ? String(value) : '—'
}

// 展示层兜底清洗（老切块在重灌前还带 markdown 图片/URL 垃圾）
function cleanContent(raw: string) {
  return raw
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/https?:\/\/\S+/g, ' ')
    .replace(/[*#`_]{1,}/g, '')
    .replace(/\s{2,}/g, ' ')
    .trim()
}

const expandedHits = ref<Record<number, boolean>>({})
function toggleHit(id: number) {
  expandedHits.value[id] = !expandedHits.value[id]
}

const uploading = ref<{ id: number; filename: string; status: string; chunk_count: number; error: string | null }[]>([])
const fileInput = ref<HTMLInputElement | null>(null)
const uploadPollTimers = new Map<number, ReturnType<typeof setInterval>>()
const dragOver = ref(false)
const MAX_UPLOAD_SIZE = 20 * 1024 * 1024  // 20MB，与后端一致
const ALLOWED_EXTS = ['.pdf', '.docx', '.pptx', '.txt', '.md']

function clearUploadPoll(uploadId: number) {
  const t = uploadPollTimers.get(uploadId)
  if (t) { clearInterval(t); uploadPollTimers.delete(uploadId) }
}

function pollUploadStatus(uploadId: number) {
  uploadPollTimers.set(uploadId, setInterval(async () => {
    try {
      const s = await api.knowledgeUploadStatus(uploadId)
      const idx = uploading.value.findIndex(u => u.id === uploadId)
      if (idx >= 0) uploading.value[idx] = s
      if (s.status === 'indexed' || s.status === 'failed' || s.status === 'empty') {
        clearUploadPoll(uploadId)
        if (s.status === 'indexed') loadDocuments()
      }
    } catch {
      clearUploadPoll(uploadId)
    }
  }, 3000))
}

async function handleFiles(files: FileList | null) {
  if (!files || !files.length) return
  for (const file of Array.from(files)) {
    // 客户端预检：大小
    if (file.size > MAX_UPLOAD_SIZE) {
      uploading.value.push({ id: 0, filename: file.name, status: 'failed', chunk_count: 0, error: `文件过大（${(file.size / 1024 / 1024).toFixed(1)}MB > 20MB）` })
      continue
    }
    // 客户端预检：扩展名
    const ext = '.' + file.name.split('.').pop()?.toLowerCase()
    if (!ALLOWED_EXTS.includes(ext)) {
      uploading.value.push({ id: 0, filename: file.name, status: 'failed', chunk_count: 0, error: `不支持的类型 ${ext}，仅限 PDF/Word/PPTX/txt/md` })
      continue
    }
    try {
      const res = await api.knowledgeUpload(file)
      const entry: { id: number; filename: string; status: string; chunk_count: number; error: string | null } = { id: res.upload_id, filename: file.name, status: 'parsing', chunk_count: 0, error: null }
      uploading.value.push(entry)
      pollUploadStatus(res.upload_id)
    } catch (err) {
      uploading.value.push({ id: 0, filename: file.name, status: 'failed', chunk_count: 0, error: err instanceof Error ? err.message : '上传失败' })
    }
  }
  if (fileInput.value) fileInput.value.value = ''
}

function triggerUpload() { fileInput.value?.click() }
function onDragOver(e: DragEvent) { e.preventDefault(); e.stopPropagation(); dragOver.value = true }
function onDragLeave() { dragOver.value = false }
function onDrop(e: DragEvent) { e.preventDefault(); e.stopPropagation(); dragOver.value = false; handleFiles(e.dataTransfer?.files ?? null) }
function removeUpload(uploadId: number) { clearUploadPoll(uploadId); uploading.value = uploading.value.filter(u => u.id !== uploadId) }

onMounted(() => {
  loadStats()
  loadDocuments()
})

onUnmounted(() => {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
  uploadPollTimers.forEach(clearUploadPoll)
  uploadPollTimers.clear()
})
</script>

<template>
  <section class="page-section">
    <header class="section-header">
      <div>
        <h2>知识库</h2>
        <p>内部语料（新闻缓存 + 数字人报告）切块→嵌入→向量索引，数字人回答可语义引用。</p>
      </div>
      <button type="button" :disabled="reindexing" @click="reindex()">
        {{ reindexing ? '索引中…' : '重建索引' }}
      </button>
    </header>

    <div v-if="stats" class="kb-cards">
      <div class="kb-card"><b>{{ stats.documents }}</b><span>文档</span></div>
      <div class="kb-card"><b>{{ stats.chunks }}</b><span>切块</span></div>
      <div class="kb-card gold" :class="{ 'vector-error': !stats.vector_healthy }" :title="stats.vector_error || undefined">
        <b>{{ stats.vector_pct }}%</b><span>向量 {{ stats.vector_count }}/{{ stats.searchable_chunks }} 块</span>
      </div>
      <div class="kb-card"><b>{{ stats.backend }}</b><span>{{ stats.collection }}{{ stats.dim ? ' · ' + stats.dim + '维' : '' }}</span></div>
    </div>

    <div v-if="stats?.ingest.running" class="kb-progress">
      <div class="kb-progress-bar"><i :style="{ width: ingestPct + '%' }"></i></div>
      <span>摄取中 {{ stats.ingest.processed }}/{{ stats.ingest.total }}（新索引 {{ stats.ingest.indexed }} 篇，失败 {{ stats.ingest.failed }} 块）</span>
    </div>
    <p v-if="reindexMsg" class="hint">{{ reindexMsg }}</p>
    <p v-if="error" class="error-text">{{ error }}</p>

    <div class="settings-tabs kb-tabs">
      <button type="button" :class="{ active: activeTab === 'docs' }" @click="activeTab = 'docs'">文档列表</button>
      <button type="button" :class="{ active: activeTab === 'search' }" @click="activeTab = 'search'">召回测试</button>
    </div>

    <template v-if="activeTab === 'docs'">
      <div class="kb-upload-zone" :class="{ 'drag-over': dragOver }"
           @click="triggerUpload"
           @dragover="onDragOver"
           @dragleave="onDragLeave"
           @drop="onDrop">
        <input ref="fileInput" type="file" accept=".pdf,.docx,.pptx,.txt,.md" multiple style="display:none" @change="e => handleFiles((e.target as HTMLInputElement).files)" />
        <span class="kb-upload-icon">⬆</span>
        <span>拖拽上传 <b>PDF / Word / PPTX / txt / md</b>（≤20MB），或点击选择文件</span>
      </div>
      <div v-if="uploading.length" class="kb-upload-list">
        <div v-for="u in uploading" :key="u.id" class="kb-upload-item" :class="u.status">
          <span class="kb-upload-name">{{ u.filename }}</span>
          <span class="kb-upload-status">
            <template v-if="u.status === 'parsing'">解析中…</template>
            <template v-else-if="u.status === 'indexed'">✓ 已索引 {{ u.chunk_count }} 块</template>
            <template v-else-if="u.status === 'empty'">⚠ 正文过短，未产生切块</template>
            <template v-else-if="u.status === 'failed'">✗ {{ u.error || '失败' }}</template>
            <template v-else>{{ u.status }}</template>
          </span>
          <button type="button" class="kb-upload-remove" @click="removeUpload(u.id)" title="移除">×</button>
        </div>
      </div>

      <div class="kb-filters">
        <div class="kb-chips">
          <button
            v-for="chip in typeChips"
            :key="chip.key"
            type="button"
            class="kb-chip"
            :class="{ on: filterDocType === chip.key }"
            @click="pickType(chip.key)"
          >
            {{ chip.label }} {{ chip.count }}
          </button>
        </div>
        <select v-model="filterTrust" @change="page = 1; loadDocuments()">
          <option value="">全部可信度</option>
          <option value="trusted">可信</option>
          <option value="normal">外部</option>
          <option value="untrusted">未核实</option>
        </select>
        <input v-model="keyword" placeholder="按标题搜索" @keyup.enter="page = 1; loadDocuments()" />
        <button type="button" :disabled="loading" @click="page = 1; loadDocuments()">
          {{ loading ? '加载中' : '查询' }}
        </button>
      </div>

      <div v-if="!documents.length && !loading" class="kb-empty">暂无文档，点右上「重建索引」摄取内部语料。</div>
      <div v-else class="table-wrap">
        <table class="list-table kb-documents-table">
          <colgroup>
            <col class="kb-col-title" />
            <col class="kb-col-type" />
            <col class="kb-col-source" />
            <col class="kb-col-trust" />
            <col class="kb-col-time" />
            <col class="kb-col-chunks" />
            <col class="kb-col-status" />
            <col class="kb-col-actions" />
          </colgroup>
          <thead>
            <tr>
              <th>标题</th><th>类型</th><th>来源</th><th>可信度</th><th>入库时间</th><th>切块</th><th>状态</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="d in documents" :key="d.id">
              <tr>
                <td class="kb-doc-title-cell"><strong class="kb-doc-title" :title="d.title">{{ displayTitle(d.title) }}</strong></td>
                <td class="nowrap">{{ typeLabel(d.doc_type) }}</td>
                <td class="nowrap">{{ d.source }}</td>
                <td class="nowrap"><span class="kb-pill" :class="trustMeta(d.trust_level).cls">{{ trustMeta(d.trust_level).label }}</span></td>
                <td class="nowrap">{{ beijingFromDb(d.fetched_at) }}</td>
                <td class="nowrap">{{ d.chunk_count }}</td>
                <td class="nowrap"><span class="kb-pill" :class="statusMeta(d.status).cls">{{ statusMeta(d.status).label }}</span></td>
                <td>
                  <div class="cell-actions">
                    <button type="button" :disabled="loadingChunks === d.id" @click="loadChunks(d)">
                      {{ chunks[d.id] ? '收起' : '切块' }}
                    </button>
                    <button type="button" :disabled="reindexing" @click="reindex(d.id)">重灌</button>
                    <button v-if="d.status === 'indexed'" type="button" class="danger" @click="disableDoc(d)">禁用</button>
                  </div>
                </td>
              </tr>
              <tr v-if="chunks[d.id]">
                <td colspan="8" class="kb-chunks-cell">
                  <div v-for="c in chunks[d.id]" :key="c.id" class="kb-chunk">
                    <span>#{{ c.chunk_index }} · {{ c.token_count }}字 · 命中{{ c.hit_count }} · {{ c.status }}</span>
                    <p>{{ c.content }}</p>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>

      <div class="kb-pager" v-if="documents.length">
        <button type="button" :disabled="page <= 1" @click="page--; loadDocuments()">上一页</button>
        <span>第 {{ page }} 页 / 共 {{ Math.ceil(total / pageSize) || 1 }} 页（{{ total }} 条）</span>
        <button type="button" :disabled="page * pageSize >= total" @click="page++; loadDocuments()">下一页</button>
      </div>
    </template>

    <template v-else>
      <div class="kb-search-wrap">
        <div class="kb-search-bar">
          <input
            v-model="searchQuery"
            class="kb-search-input"
            placeholder="输入自然问句，如：宁德时代盈利能力怎么样"
            @keyup.enter="runSearch"
          />
          <label class="kb-topk">top_k<input type="number" v-model.number="searchTopK" min="1" max="20" /></label>
          <button type="button" class="kb-search-btn" :disabled="searching" @click="runSearch">
            {{ searching ? '检索中…' : '检 索' }}
          </button>
        </div>
        <p class="kb-search-hint">向量 + 关键词双路召回 → RRF 融合 → 重排精排；分数条为本次结果内的相对强度。</p>

        <div v-if="searched && !searchHits.length" class="kb-empty">无命中。确认索引已建（统计卡切块数 &gt; 0）或换个问法。</div>

        <article v-for="(h, i) in searchHits" :key="h.chunk_id" class="kb-hit">
          <div class="kb-hit-head">
            <span class="kb-rank">{{ i + 1 }}</span>
            <strong class="kb-hit-title">{{ h.title }}</strong>
            <span class="kb-pill pill-gray">{{ typeLabel(h.doc_type) }}</span>
          </div>
          <div class="kb-hit-meta">
            <span>{{ h.source }} · 片段 #{{ h.chunk_index }}</span>
            <a v-if="h.source_url" :href="h.source_url" target="_blank" rel="noopener">原文 ↗</a>
          </div>
          <div class="kb-scores">
            <span class="kb-score">向量 <i class="kb-bar"><b class="v" :style="{ width: barWidth(h, 'vector_score') + '%' }"></b></i> <em>{{ fmtScore(h.vector_score) }}</em></span>
            <span class="kb-score">关键词 <i class="kb-bar"><b class="k" :style="{ width: barWidth(h, 'keyword_score') + '%' }"></b></i> <em>{{ fmtScore(h.keyword_score) }}</em></span>
            <span class="kb-score">融合 <i class="kb-bar"><b class="f" :style="{ width: barWidth(h, 'fused_score') + '%' }"></b></i> <em>{{ fmtScore(h.fused_score) }}</em></span>
            <span class="kb-score">重排 <i class="kb-bar"><b class="r" :style="{ width: barWidth(h, 'rerank_score') + '%' }"></b></i> <em>{{ fmtScore(h.rerank_score) }}</em></span>
          </div>
          <p
            class="kb-hit-content"
            :class="{ clamped: !expandedHits[h.chunk_id] }"
            :title="expandedHits[h.chunk_id] ? '点击收起' : '点击展开全文'"
            @click="toggleHit(h.chunk_id)"
          >{{ cleanContent(h.content) }}</p>
        </article>
      </div>
    </template>
  </section>
</template>

<style scoped>
.kb-cards { display: flex; gap: 10px; flex-wrap: wrap; padding: 8px 0 4px; }
.kb-card { min-width: 130px; padding: 8px 14px; border: 1px solid var(--line); border-radius: 8px; background: rgba(213, 180, 106, 0.03); }
.kb-card b { display: block; font-size: 19px; color: var(--text); font-variant-numeric: tabular-nums; }
.kb-card span { font-size: 11px; color: var(--muted); }
.kb-card.gold b { color: var(--gold-2); }
.kb-progress { display: flex; align-items: center; gap: 10px; padding: 6px 0; font-size: 12px; color: var(--muted); }
.kb-progress-bar { width: 220px; height: 6px; border-radius: 3px; background: rgba(213, 180, 106, 0.12); overflow: hidden; }
.kb-progress-bar i { display: block; height: 100%; background: var(--gold-2); border-radius: 3px; transition: width 0.6s ease; }
.kb-tabs { margin: 6px 0 10px; }
.kb-upload { padding: 12px; margin-bottom: 10px; border: 1.5px dashed rgba(213, 180, 106, 0.35); border-radius: 8px; color: var(--muted); font-size: 12.5px; text-align: center; background: rgba(213, 180, 106, 0.03); cursor: not-allowed; }
.kb-upload b { color: var(--gold-2); }
.kb-filters { display: flex; gap: 8px; padding: 4px 0 10px; align-items: center; flex-wrap: wrap; }
.kb-chips { display: flex; gap: 6px; flex-wrap: wrap; }
.kb-chip { font-size: 11.5px; padding: 2px 10px; border-radius: 999px; border: 1px solid var(--line); background: transparent; color: var(--muted); cursor: pointer; }
.kb-chip.on { background: rgba(213, 180, 106, 0.16); color: var(--gold-2); border-color: rgba(213, 180, 106, 0.5); }
.kb-empty { padding: 16px; color: var(--muted); font-size: 13px; }
.kb-pill { display: inline-block; font-size: 10.5px; padding: 1px 8px; border-radius: 999px; white-space: nowrap; }
.pill-green { color: #6db985; background: rgba(109, 185, 133, 0.12); border: 1px solid rgba(109, 185, 133, 0.32); }
.pill-blue { color: #6ea8d8; background: rgba(110, 168, 216, 0.1); border: 1px solid rgba(110, 168, 216, 0.3); }
.pill-red { color: #e06c6c; background: rgba(224, 108, 108, 0.1); border: 1px solid rgba(224, 108, 108, 0.32); }
.pill-gray { color: var(--muted); background: rgba(168, 159, 138, 0.08); border: 1px solid rgba(168, 159, 138, 0.28); }
.kb-chunks-cell { background: rgba(12, 12, 13, 0.35); padding: 8px 12px; }
.kb-chunk { font-size: 12px; margin: 2px 0; }
.kb-chunk p { margin: 2px 0 6px; }
.kb-pager { display: flex; align-items: center; gap: 12px; padding: 8px 0; font-size: 13px; color: var(--muted); }
.kb-search-wrap { max-width: 940px; }
.kb-search-bar { display: flex; gap: 10px; align-items: stretch; padding: 6px 0 2px; }
.kb-search-input { flex: 1; min-width: 0; padding: 10px 14px; font-size: 14px; background: rgba(12, 12, 13, 0.6); border: 1px solid var(--line-strong); border-radius: 8px; color: var(--text); }
.kb-search-input:focus { border-color: var(--gold-2); outline: none; }
.kb-topk { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); border: 1px solid var(--line); border-radius: 8px; padding: 0 4px 0 12px; }
.kb-topk input { width: 46px; background: transparent; border: 0; color: var(--gold-2); font-size: 14px; text-align: center; }
.kb-topk input:focus { outline: none; }
.kb-search-btn { padding: 0 24px; border: 0; border-radius: 8px; background: linear-gradient(135deg, #d5b46a, #b8933f); color: #141414; font-weight: 700; font-size: 13px; letter-spacing: 0.1em; cursor: pointer; }
.kb-search-btn:disabled { opacity: 0.55; cursor: wait; }
.kb-search-hint { margin: 8px 0 16px; font-size: 12px; color: var(--muted); }
.kb-hit { padding: 14px 16px; border: 1px solid var(--line); border-radius: 10px; margin: 0 0 12px; font-size: 12px; background: rgba(213, 180, 106, 0.02); transition: border-color 0.15s ease; }
.kb-hit:hover { border-color: rgba(213, 180, 106, 0.45); }
.kb-hit-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.kb-rank { flex: none; font-variant-numeric: tabular-nums; font-size: 12px; color: var(--gold-2); border: 1px solid rgba(213, 180, 106, 0.4); border-radius: 6px; padding: 0 7px; line-height: 1.7; }
.kb-hit-title { font-size: 14.5px; color: var(--text); line-height: 1.45; overflow-wrap: anywhere; }
.kb-hit-meta { display: flex; align-items: center; gap: 12px; margin: 6px 0 0 34px; font-size: 12px; color: var(--muted); }
.kb-hit-meta a { color: #6ea8d8; text-decoration: none; }
.kb-scores { display: flex; gap: 24px; flex-wrap: wrap; margin: 12px 0 0 34px; color: var(--muted); font-size: 11px; }
.kb-score { display: flex; align-items: center; gap: 7px; }
.kb-score em { font-style: normal; font-variant-numeric: tabular-nums; color: var(--text); font-size: 12px; }
.kb-bar { display: inline-block; width: 96px; height: 6px; border-radius: 3px; background: rgba(213, 180, 106, 0.1); overflow: hidden; }
.kb-bar b { display: block; height: 100%; border-radius: 3px; }
.kb-bar .v { background: var(--gold-2); }
.kb-bar .k { background: #6ea8d8; }
.kb-bar .f { background: #a89f8a; }
.kb-bar .r { background: #6db985; }
.kb-hit-content { margin: 10px 0 0 34px; overflow-wrap: anywhere; cursor: pointer; font-size: 13px; line-height: 1.75; color: var(--text); opacity: 0.88; }
.kb-hit-content.clamped { display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.hint { font-size: 12px; color: var(--gold-2); }
.error-text { color: #e06c6c; font-size: 13px; }
.kb-upload-zone { display: flex; align-items: center; gap: 10px; padding: 12px 16px; border: 1px dashed var(--line-strong); border-radius: 10px; background: rgba(12, 12, 13, 0.35); cursor: pointer; transition: border-color 0.15s ease, background 0.15s ease; }
.kb-upload-zone:hover { border-color: var(--gold-2); background: rgba(213, 180, 106, 0.05); }
.kb-upload-zone.drag-over { border-color: var(--gold-2); background: rgba(213, 180, 106, 0.1); border-style: solid; }
.kb-upload-icon { font-size: 18px; }
.kb-upload-list { margin: 8px 0; display: grid; gap: 4px; }
.kb-upload-item { display: flex; align-items: center; gap: 10px; padding: 6px 12px; border-radius: 8px; font-size: 12px; background: rgba(12, 12, 13, 0.5); }
.kb-upload-item.parsing { color: var(--gold-2); }
.kb-upload-item.indexed { color: #6db985; }
.kb-upload-item.empty { color: #b8a65c; }
.kb-upload-item.failed { color: #e06c6c; }
.kb-upload-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.kb-upload-status { flex: none; font-size: 11px; }
.kb-upload-remove { flex: none; background: transparent; border: 0; color: var(--muted); cursor: pointer; font-size: 16px; padding: 0 4px; line-height: 1; }
.kb-upload-remove:hover { color: var(--text); }
.kb-documents-table { min-width: 1060px; table-layout: fixed; }
.kb-col-title { width: 40%; }
.kb-col-type { width: 6%; }
.kb-col-source { width: 8%; }
.kb-col-trust { width: 7%; }
.kb-col-time { width: 12%; }
.kb-col-chunks { width: 5%; }
.kb-col-status { width: 7%; }
.kb-col-actions { width: 15%; }
.kb-doc-title-cell { min-width: 0; }
.kb-doc-title { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.kb-card.vector-error { border-color: var(--danger); }
</style>
