<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
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
        <p>内部语料（新闻缓存 + 数字人报告）切块→嵌入→向量索引，数字人回答可语义引用。</p>
      </div>
      <button type="button" :disabled="reindexing" @click="reindex()">
        {{ reindexing ? '索引中…' : '重建索引' }}
      </button>
    </header>

    <div v-if="stats" class="kb-cards">
      <div class="kb-card"><b>{{ stats.documents }}</b><span>文档</span></div>
      <div class="kb-card"><b>{{ stats.chunks }}</b><span>切块</span></div>
      <div class="kb-card gold"><b>{{ stats.indexed_pct }}%</b><span>已索引 {{ stats.indexed_docs }} 篇</span></div>
      <div class="kb-card"><b>{{ stats.backend }}</b><span>{{ stats.collection }} · {{ stats.dim || '—' }}维</span></div>
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
      <div class="kb-upload" title="P2 阶段开放">
        ⬆ 拖拽上传 <b>PDF / Word / txt / md</b> —— P2 开放；当前自动摄取内部语料（搜索缓存 + 数字人报告）
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
        <table class="list-table">
          <thead>
            <tr>
              <th>标题</th><th>类型</th><th>来源</th><th>可信度</th><th>切块</th><th>状态</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="d in documents" :key="d.id">
              <tr>
                <td><strong>{{ d.title }}</strong></td>
                <td class="nowrap">{{ typeLabel(d.doc_type) }}</td>
                <td class="nowrap">{{ d.source }}</td>
                <td class="nowrap"><span class="kb-pill" :class="trustMeta(d.trust_level).cls">{{ trustMeta(d.trust_level).label }}</span></td>
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
                <td colspan="7" class="kb-chunks-cell">
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
      <div class="kb-search-bar">
        <input v-model="searchQuery" placeholder="输入检索问句，如：宁德时代盈利能力怎么样" @keyup.enter="runSearch" />
        <label>top_k <input type="number" v-model.number="searchTopK" min="1" max="20" /></label>
        <button type="button" :disabled="searching" @click="runSearch">{{ searching ? '检索中' : '检索' }}</button>
      </div>
      <div v-if="searched && !searchHits.length" class="kb-empty">无命中。确认索引已建（统计卡切块数 &gt; 0）或换个问法。</div>
      <div v-for="h in searchHits" :key="h.chunk_id" class="kb-hit">
        <div class="kb-hit-head">
          <strong>{{ h.title }}</strong>
          <span class="kb-pill pill-gray">{{ typeLabel(h.doc_type) }}</span>
          <span class="muted">{{ h.source }}</span>
          <a v-if="h.source_url" :href="h.source_url" target="_blank" rel="noopener">原文 ↗</a>
        </div>
        <div class="kb-scores">
          <span class="kb-score">向量 <i class="kb-bar"><b class="v" :style="{ width: barWidth(h, 'vector_score') + '%' }"></b></i> {{ h.vector_score }}</span>
          <span class="kb-score">关键词 <i class="kb-bar"><b class="k" :style="{ width: barWidth(h, 'keyword_score') + '%' }"></b></i> {{ h.keyword_score }}</span>
          <span class="kb-score">融合 <i class="kb-bar"><b class="f" :style="{ width: barWidth(h, 'fused_score') + '%' }"></b></i> {{ h.fused_score }}</span>
          <span class="kb-score">重排 <i class="kb-bar"><b class="r" :style="{ width: barWidth(h, 'rerank_score') + '%' }"></b></i> {{ h.rerank_score ?? '—' }}</span>
        </div>
        <p class="kb-hit-content">{{ h.content }}</p>
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
.kb-search-bar { display: flex; gap: 8px; align-items: center; padding: 4px 0 10px; }
.kb-search-bar input[type='number'] { width: 64px; }
.kb-hit { padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px; margin: 6px 0; font-size: 12px; }
.kb-hit-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.kb-hit-head .muted { color: var(--muted); }
.kb-hit-head a { color: #6ea8d8; text-decoration: none; font-size: 11.5px; }
.kb-scores { display: flex; gap: 16px; flex-wrap: wrap; margin: 6px 0 2px; color: var(--muted); font-size: 11px; }
.kb-score { display: flex; align-items: center; gap: 6px; font-variant-numeric: tabular-nums; }
.kb-bar { display: inline-block; width: 72px; height: 5px; border-radius: 3px; background: rgba(213, 180, 106, 0.1); overflow: hidden; }
.kb-bar b { display: block; height: 100%; border-radius: 3px; }
.kb-bar .v { background: var(--gold-2); }
.kb-bar .k { background: #6ea8d8; }
.kb-bar .f { background: #a89f8a; }
.kb-bar .r { background: #6db985; }
.kb-hit-content { margin: 6px 0 0; }
.hint { font-size: 12px; color: var(--gold-2); }
.error-text { color: #e06c6c; font-size: 13px; }
</style>
