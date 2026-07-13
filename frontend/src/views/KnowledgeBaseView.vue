<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api'
import type { KnowledgeStats, KnowledgeDocument, KnowledgeChunk, KnowledgeSearchHit } from '../types'

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
const searchQuery = ref('')
const searchTopK = ref(6)
const searchHits = ref<KnowledgeSearchHit[]>([])
const searching = ref(false)
let pollTimer: ReturnType<typeof setInterval> | null = null
let lastPct = -1
let stableCount = 0

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

function stopReindex(msg: string) {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
  reindexing.value = false
  reindexMsg.value = msg
  lastPct = -1
  stableCount = 0
  loadDocuments()
}

async function reindex(docId?: number) {
  reindexing.value = true
  reindexMsg.value = ''
  try {
    const res = await api.knowledgeReindex(docId)
    reindexMsg.value = `已启动重建（${res.scope}），进度轮询统计…`
    pollTimer = setInterval(async () => {
      await loadStats()
      const pct = stats.value?.indexed_pct ?? 0
      if (pct >= 99.9) {
        stopReindex('重建完成')
      } else if (Math.abs(pct - lastPct) < 0.01) {
        stableCount += 1
        if (stableCount >= 3) {
          stopReindex('进度连续 3 轮无变化（可能已完成或失败，查看后端日志）')
        }
      } else {
        stableCount = 0
        lastPct = pct
      }
    }, 5000)
    // 兜底超时 10min
    setTimeout(() => {
      if (pollTimer) stopReindex('重建超时（10min），请查看后端日志或 stats')
    }, 600000)
  } catch (err) {
    reindexMsg.value = err instanceof Error ? err.message : '重建失败'
    reindexing.value = false
  }
  // 单文档重灌后清掉该文档的切块缓存，下次展开重新拉
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
  } catch (err) {
    error.value = err instanceof Error ? err.message : '检索失败'
  } finally {
    searching.value = false
  }
}

onMounted(() => {
  loadStats()
  loadDocuments()
})

onUnmounted(() => {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
})
</script>

<template>
  <section class="page-section">
    <header class="section-header">
      <div>
        <h2>知识库</h2>
        <p>内部语料（新闻缓存 + 数字人报告）切块→嵌入→向量索引，可语义检索。</p>
      </div>
      <button type="button" :disabled="reindexing" @click="reindex()">
        {{ reindexing ? '索引中…' : '重建索引' }}
      </button>
    </header>

    <div v-if="stats" class="kb-stat-row">
      <span>文档 {{ stats.documents }}</span>
      <span>切块 {{ stats.chunks }}</span>
      <span>已索引 {{ stats.indexed_docs }}（{{ stats.indexed_pct }}%）</span>
      <span>{{ stats.backend }} · {{ stats.collection }} · {{ stats.dim || '—' }}维</span>
    </div>
    <p v-if="reindexMsg" class="hint">{{ reindexMsg }}</p>
    <p v-if="error" class="error-text">{{ error }}</p>

    <div class="kb-filters">
      <input v-model="keyword" placeholder="按标题搜索" @keyup.enter="page = 1; loadDocuments()" />
      <select v-model="filterDocType" @change="page = 1; loadDocuments()">
        <option value="">全部类型</option>
        <option value="news">新闻</option>
        <option value="report">报告</option>
      </select>
      <select v-model="filterTrust" @change="page = 1; loadDocuments()">
        <option value="">全部可信度</option>
        <option value="trusted">可信</option>
        <option value="normal">普通</option>
        <option value="untrusted">未核实</option>
      </select>
      <button type="button" :disabled="loading" @click="page = 1; loadDocuments()">
        {{ loading ? '加载中' : '查询' }}
      </button>
    </div>

    <div v-if="!documents.length && !loading" class="kb-empty">暂无文档，点右上「重建索引」摄取内部语料。</div>
    <div v-else class="kb-table">
      <div class="kb-head">
        <span>标题</span><span>类型</span><span>来源</span><span>可信度</span><span>切块</span><span>状态</span><span>操作</span>
      </div>
      <div v-for="d in documents" :key="d.id" class="kb-row">
        <span><strong>{{ d.title }}</strong></span>
        <span>{{ d.doc_type }}</span>
        <span>{{ d.source }}</span>
        <span>{{ d.trust_level }}</span>
        <span>{{ d.chunk_count }}</span>
        <span>{{ d.indexed ? '✅索引' : '—' }}</span>
        <span class="cell-actions">
          <button type="button" :disabled="loadingChunks === d.id" @click="loadChunks(d)">
            {{ chunks[d.id] ? '收起' : '切块' }}
          </button>
          <button type="button" :disabled="reindexing" @click="reindex(d.id)">重灌</button>
        </span>
        <section v-if="chunks[d.id]" class="kb-chunks">
          <div v-for="c in chunks[d.id]" :key="c.id" class="kb-chunk">
            <span>#{{ c.chunk_index }} · {{ c.token_count }}字 · 命中{{ c.hit_count }} · {{ c.status }}</span>
            <p>{{ c.content }}</p>
          </div>
        </section>
      </div>
    </div>

    <div class="kb-pager" v-if="documents.length">
      <button type="button" :disabled="page <= 1" @click="page--; loadDocuments()">上一页</button>
      <span>第 {{ page }} 页 / 共 {{ Math.ceil(total / pageSize) || 1 }} 页（{{ total }} 条）</span>
      <button type="button" :disabled="page * pageSize >= total" @click="page++; loadDocuments()">下一页</button>
    </div>

    <section class="kb-search">
      <h3>召回测试</h3>
      <div class="kb-search-bar">
        <input v-model="searchQuery" placeholder="输入检索问句，如 宁德时代盈利" @keyup.enter="runSearch" />
        <label>top_k <input type="number" v-model.number="searchTopK" min="1" max="20" /></label>
        <button type="button" :disabled="searching" @click="runSearch">{{ searching ? '检索中' : '检索' }}</button>
      </div>
      <div v-if="!searchHits.length && !searching" class="kb-empty">输入问句检索，看向量/关键词/融合/重排四路分数。</div>
      <div v-for="h in searchHits" :key="h.chunk_id" class="kb-hit">
        <div class="kb-hit-head">
          <strong>{{ h.title }}</strong> · {{ h.doc_type }} · {{ h.source }}
          <a v-if="h.source_url" :href="h.source_url" target="_blank" rel="noopener">原文</a>
        </div>
        <div class="kb-scores">
          <span>向量 {{ h.vector_score }}</span>
          <span>关键词 {{ h.keyword_score }}</span>
          <span>融合 {{ h.fused_score }}</span>
          <span>重排 {{ h.rerank_score ?? '—' }}</span>
        </div>
        <p class="kb-hit-content">{{ h.content }}</p>
      </div>
    </section>
  </section>
</template>

<style scoped>
.kb-stat-row { display: flex; gap: 16px; padding: 8px 0; font-size: 13px; color: var(--muted); }
.kb-filters { display: flex; gap: 8px; padding: 8px 0; align-items: center; }
.kb-empty { padding: 16px; color: var(--muted); font-size: 13px; }
.kb-table { display: grid; gap: 2px; }
.kb-head, .kb-row { display: grid; grid-template-columns: 1.6fr 0.6fr 0.8fr 0.7fr 0.5fr 0.7fr 1fr; gap: 8px; align-items: start; padding: 8px; border-bottom: 1px solid rgba(255,255,255,0.08); }
.kb-head { font-size: 12px; color: var(--muted); }
.cell-actions { display: flex; gap: 6px; }
.cell-actions button { font-size: 12px; padding: 2px 8px; }
.kb-chunks { grid-column: 1 / -1; display: grid; gap: 6px; padding: 8px; background: rgba(12,12,13,0.5); border-radius: 8px; margin-top: 4px; }
.kb-chunk { font-size: 12px; }
.kb-chunk p { margin: 2px 0 6px; color: var(--text); }
.kb-pager { display: flex; align-items: center; gap: 12px; padding: 8px 0; font-size: 13px; color: var(--muted); }
.kb-search { margin-top: 16px; padding-top: 12px; border-top: 1px solid rgba(255,255,255,0.08); }
.kb-search-bar { display: flex; gap: 8px; align-items: center; }
.kb-hit { padding: 8px; border: 1px solid rgba(255,255,255,0.08); border-radius: 6px; margin: 6px 0; font-size: 12px; }
.kb-scores { display: flex; gap: 12px; color: var(--muted); margin: 2px 0; }
.kb-hit-content { margin: 4px 0 0; }
.hint { font-size: 12px; color: #d5b46a; }
.error-text { color: #e06c6c; font-size: 13px; }
</style>
