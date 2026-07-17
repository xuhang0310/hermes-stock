<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'
import type { WorkspaceDoc } from '../types'

const docs = ref<WorkspaceDoc[]>([])
const loading = ref(false)
const error = ref('')
const busyPath = ref<string | null>(null) // 正在下载/入库的文档 path
const notice = ref('')

// 数字人 code → 中文名（稳定，内联即可）
const DH_NAMES: Record<string, string> = {
  'stock-pm': 'A股研究总调度',
  'risk-officer': '风控复盘官',
  'market-sentinel': '盘中哨兵',
  'research-analyst': '个股研究员',
  'intelligence-officer': '情报官',
  'quant-analyst': '量化分析师',
  'macro-strategist': '宏观策略师',
  'short-term-hunter': '短线猎手',
  ask_stock: '问股助手'
}

function dhName(code: string | null) {
  return code ? DH_NAMES[code] || code : ''
}

function fmtBytes(n: number) {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

function fmtTime(epochSec: number) {
  const d = new Date(epochSec * 1000)
  const p = (x: number) => String(x).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    docs.value = await api.workspaceDocuments()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

async function download(doc: WorkspaceDoc) {
  busyPath.value = doc.path
  try {
    await api.downloadWorkspaceDoc(doc.path, doc.name)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '下载失败'
  } finally {
    busyPath.value = null
  }
}

async function ingest(doc: WorkspaceDoc) {
  busyPath.value = doc.path
  notice.value = ''
  try {
    const res = await api.ingestWorkspaceDoc(doc.path)
    notice.value = `《${doc.title}》已加入知识库，切块 ${res.chunks} 段。`
    await load()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加入知识库失败'
  } finally {
    busyPath.value = null
  }
}

onMounted(load)
</script>

<template>
  <section class="page-section ws-docs">
    <header class="ws-docs-head">
      <div>
        <h3>生成文档</h3>
        <p class="hint">数字人生成并存入工作区的文档：可下载、可加入知识库供全体数字人检索。</p>
      </div>
      <button class="ghost-btn" :disabled="loading" @click="load">{{ loading ? '刷新中…' : '刷新' }}</button>
    </header>

    <p v-if="notice" class="ws-notice">{{ notice }}</p>
    <p v-if="error" class="chat-error">{{ error }}</p>

    <div v-if="!loading && !docs.length" class="ws-empty">
      还没有生成的文档。在数字人对话里让它生成研究/报告，点回复下方「存入工作区」即可。
    </div>

    <table v-else-if="docs.length" class="ws-table">
      <thead>
        <tr>
          <th>文档</th>
          <th>关联对话</th>
          <th>大小</th>
          <th>时间</th>
          <th>知识库</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="doc in docs" :key="doc.path">
          <td>
            <strong class="ws-title">{{ doc.title }}</strong>
            <span class="ws-path">{{ doc.path }}</span>
          </td>
          <td>
            <template v-if="doc.session">
              <span class="ws-session">{{ doc.session.title }}</span>
              <span v-if="doc.digital_human_code" class="pill pill-gray">{{ dhName(doc.digital_human_code) }}</span>
            </template>
            <span v-else class="muted">—（Agent 直接生成）</span>
          </td>
          <td>{{ fmtBytes(doc.bytes) }}</td>
          <td>{{ fmtTime(doc.mtime) }}</td>
          <td>
            <span v-if="doc.ingested" class="pill pill-green">✓ 已入库</span>
            <span v-else class="pill pill-gray">未入库</span>
          </td>
          <td class="ws-actions">
            <button class="ghost-btn" :disabled="busyPath === doc.path" @click="download(doc)">⬇ 下载</button>
            <button
              class="ghost-btn"
              :disabled="busyPath === doc.path || doc.ingested"
              @click="ingest(doc)"
            >
              {{ doc.ingested ? '已入库' : busyPath === doc.path ? '处理中…' : '加入知识库' }}
            </button>
          </td>
        </tr>
      </tbody>
    </table>
  </section>
</template>
