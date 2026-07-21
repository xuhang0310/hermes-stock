<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import type { WorkspaceDoc } from '../types'
import { beijingFromEpoch } from '../utils/datetime'

const docs = ref<WorkspaceDoc[]>([])
const loading = ref(false)
const error = ref('')
const busyPath = ref<string | null>(null)
const notice = ref('')

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
const dhName = (code: string | null) => (code ? DH_NAMES[code] || code : '')

function ext(path: string): string {
  const m = /\.([a-z0-9]+)$/i.exec(path)
  return m ? m[1].toLowerCase() : 'txt'
}
// 文档类型 → 文件类型图标（openclaw 生成的 md/py/pdf/csv/txt 图标集）
function fileIcon(path: string): string {
  const e = ext(path)
  const key =
    e === 'md' ? 'md'
    : e === 'py' ? 'py'
    : e === 'pdf' || e === 'docx' || e === 'pptx' ? 'pdf'
    : e === 'csv' || e === 'json' || e === 'xlsx' ? 'csv'
    : 'txt'
  return `/icons/filetypes/${key}.png`
}
const avatarChar = (code: string | null) => (code ? (DH_NAMES[code] || code).slice(0, 1) : 'A')

function fmtBytes(n: number) {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}
const fmtTime = beijingFromEpoch // 东八区显示，不随浏览器时区漂移

const stats = computed(() => {
  const list = docs.value
  const ingested = list.filter((d) => d.ingested).length
  const convo = new Set(list.filter((d) => d.session).map((d) => d.session!.id)).size
  const bytes = list.reduce((sum, d) => sum + (d.bytes || 0), 0)
  return { total: list.length, ingested, convo, size: fmtBytes(bytes) }
})

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
  <section class="wsd">
    <header class="wsd-head">
      <div>
        <h2>生成<span>文档</span></h2>
        <p>数字人研究中生成、落入工作区的文档 —— 可下载原文，或一键加入知识库供全体数字人检索。</p>
      </div>
      <button class="wsd-refresh" :disabled="loading" @click="load">{{ loading ? '刷新中…' : '⟳ 刷新' }}</button>
    </header>

    <!-- 汇总统计（先总览后明细） -->
    <div class="wsd-stats">
      <div class="wsd-stat gold"><span class="k">文档总数</span><span class="v">{{ stats.total }}</span></div>
      <div class="wsd-stat ok"><span class="k">已入知识库</span><span class="v">{{ stats.ingested }}<small>/ {{ stats.total }}</small></span></div>
      <div class="wsd-stat cyan"><span class="k">关联对话</span><span class="v">{{ stats.convo }}</span></div>
      <div class="wsd-stat"><span class="k">占用空间</span><span class="v">{{ stats.size }}</span></div>
    </div>

    <p v-if="notice" class="wsd-notice">{{ notice }}</p>
    <p v-if="error" class="wsd-error">{{ error }}</p>

    <div v-if="!loading && !docs.length" class="wsd-empty">
      还没有生成的文档。在数字人对话里让它生成研究/报告并写入文件，就会出现在这里。
    </div>

    <div v-else class="wsd-list">
      <article v-for="doc in docs" :key="doc.path" class="wsd-card">
        <img class="wsd-ficon" :src="fileIcon(doc.path)" :alt="doc.name" />


        <div class="wsd-body">
          <div class="wsd-title">{{ doc.title }}</div>
          <div class="wsd-path">{{ doc.path }}</div>
          <div class="wsd-metarow">
            <span v-if="doc.session" class="wsd-convo">
              <i class="av">{{ avatarChar(doc.digital_human_code) }}</i>
              {{ doc.session.title }}<em v-if="doc.digital_human_code"> · {{ dhName(doc.digital_human_code) }}</em>
            </span>
            <span v-else class="wsd-convo agent"><i class="av">A</i>Agent 直接生成 · 无关联对话</span>
          </div>
        </div>

        <div class="wsd-right">
          <div class="wsd-meta">
            <span class="sz">{{ fmtBytes(doc.bytes) }}</span>
            <span class="tm">{{ fmtTime(doc.mtime) }}</span>
          </div>
          <span v-if="doc.ingested" class="wsd-kb in">✓ 已入库</span>
          <span v-else-if="doc.ingestable" class="wsd-kb out">未入库</span>
          <span v-else class="wsd-kb out">仅下载</span>
          <div class="wsd-actions">
            <button class="wsd-btn primary" :disabled="busyPath === doc.path" @click="download(doc)">↓ 下载</button>
            <button
              v-if="!doc.ingested && doc.ingestable"
              class="wsd-btn"
              :disabled="busyPath === doc.path"
              @click="ingest(doc)"
            >
              {{ busyPath === doc.path ? '处理中…' : '＋ 加入知识库' }}
            </button>
            <button v-else class="wsd-btn done" disabled>✓ 已加入</button>
          </div>
        </div>
      </article>
    </div>

    <p class="wsd-foot"><i class="live"></i>文件系统实时同步 · 工作区共享目录</p>
  </section>
</template>

<style scoped>
.wsd {
  --line: rgba(125, 103, 54, 0.28);
  --line-soft: rgba(125, 103, 54, 0.16);
  --card: #141317;
  --card-hi: #1a1820;
  --ink: #ece6d6;
  --ink-soft: #b8b09a;
  --ink-mute: #837c6a;
  --mono: ui-monospace, 'Cascadia Mono', 'Consolas', monospace;
}

.wsd-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; flex-wrap: wrap; }
.wsd-head h2 { margin: 0; font-size: 22px; letter-spacing: 0.5px; color: var(--ink); }
.wsd-head h2 span { color: var(--gold-2); }
.wsd-head p { margin: 6px 0 0; color: var(--ink-soft); font-size: 12.5px; max-width: 640px; }
.wsd-refresh {
  border: 1px solid var(--line); background: rgba(213, 180, 106, 0.06); color: var(--gold-2);
  border-radius: 8px; padding: 8px 14px; font-size: 12.5px; cursor: pointer; white-space: nowrap;
}
.wsd-refresh:hover:not(:disabled) { background: rgba(213, 180, 106, 0.12); border-color: var(--gold); }
.wsd-refresh:disabled { opacity: 0.6; cursor: default; }

.wsd-stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin: 20px 0; }
.wsd-stat {
  background: linear-gradient(180deg, rgba(213, 180, 106, 0.05), transparent), var(--bg-2);
  border: 1px solid var(--line-soft); border-radius: 12px; padding: 14px 16px;
  display: flex; flex-direction: column; gap: 7px;
}
.wsd-stat .k { font-size: 10.5px; letter-spacing: 1px; color: var(--ink-mute); text-transform: uppercase; }
.wsd-stat .v { font-size: 25px; font-weight: 700; letter-spacing: 0.5px; font-variant-numeric: tabular-nums; color: var(--ink); }
.wsd-stat .v small { font-size: 12px; color: var(--ink-soft); font-weight: 500; margin-left: 3px; }
.wsd-stat.gold .v { color: var(--gold-2); }
.wsd-stat.cyan .v { color: var(--cyan); }
.wsd-stat.ok .v { color: var(--ok); }

.wsd-notice {
  color: var(--ok); background: rgba(39, 197, 138, 0.08); border: 1px solid rgba(39, 197, 138, 0.25);
  border-radius: 8px; padding: 9px 13px; font-size: 13px; margin: 0 0 14px;
}
.wsd-error {
  color: var(--danger); background: rgba(255, 93, 93, 0.08); border: 1px solid rgba(255, 93, 93, 0.25);
  border-radius: 8px; padding: 9px 13px; font-size: 13px; margin: 0 0 14px;
}
.wsd-empty {
  color: var(--ink-mute); padding: 48px 16px; text-align: center;
  border: 1px dashed var(--line); border-radius: 12px;
}

.wsd-list { display: flex; flex-direction: column; gap: 12px; }
.wsd-card {
  display: grid; grid-template-columns: 50px 1fr auto; gap: 16px; align-items: center;
  background: linear-gradient(180deg, rgba(255, 255, 255, 0.015), transparent), var(--card);
  border: 1px solid var(--line-soft); border-radius: 13px; padding: 14px 18px;
  transition: transform 0.16s ease, border-color 0.16s ease, background 0.16s ease;
}
.wsd-card:hover { transform: translateY(-2px); border-color: var(--line); background: var(--card-hi); }

.wsd-ficon { width: 48px; height: 48px; flex: none; object-fit: contain; }

.wsd-body { min-width: 0; }
.wsd-title { font-size: 14.5px; font-weight: 650; color: var(--ink); letter-spacing: 0.3px; }
.wsd-path { font-family: var(--mono); font-size: 11px; color: var(--ink-mute); margin-top: 3px; word-break: break-all; }
.wsd-metarow { margin-top: 9px; }
.wsd-convo {
  display: inline-flex; align-items: center; gap: 7px; max-width: 100%;
  background: rgba(43, 212, 212, 0.08); border: 1px solid rgba(43, 212, 212, 0.3);
  border-radius: 20px; padding: 3px 11px 3px 4px; font-size: 11.5px; color: #bdeeee;
}
.wsd-convo em { color: #8fd6d6; font-style: normal; }
.wsd-convo .av {
  width: 17px; height: 17px; border-radius: 50%; flex: none; display: grid; place-items: center;
  font-size: 9px; color: #04201f; font-weight: 800; background: linear-gradient(135deg, var(--cyan), #0e6b6b);
}
.wsd-convo.agent { background: rgba(131, 124, 106, 0.08); border-color: var(--line); color: var(--ink-mute); }
.wsd-convo.agent .av { background: rgba(131, 124, 106, 0.4); color: #d8d2c1; }

.wsd-right { display: flex; align-items: center; gap: 16px; }
.wsd-meta { display: flex; flex-direction: column; gap: 3px; text-align: right; font-variant-numeric: tabular-nums; }
.wsd-meta .sz { font-size: 13px; color: var(--ink-soft); font-weight: 600; }
.wsd-meta .tm { font-size: 11px; color: var(--ink-mute); }
.wsd-kb { font-size: 11px; font-weight: 600; padding: 4px 11px; border-radius: 20px; white-space: nowrap; letter-spacing: 0.3px; }
.wsd-kb.in { color: #052e21; background: var(--ok); box-shadow: 0 0 14px rgba(39, 197, 138, 0.28); }
.wsd-kb.out { color: var(--ink-mute); background: transparent; border: 1px solid var(--line); }

.wsd-actions { display: flex; gap: 8px; }
.wsd-btn {
  font-size: 12px; padding: 7px 13px; border-radius: 8px; cursor: pointer; white-space: nowrap;
  border: 1px solid var(--gold-dim); background: transparent; color: var(--gold-2);
  transition: background 0.14s ease, border-color 0.14s ease;
}
.wsd-btn:hover:not(:disabled) { background: rgba(213, 180, 106, 0.12); border-color: var(--gold); }
.wsd-btn.primary { background: rgba(213, 180, 106, 0.14); }
.wsd-btn.done { color: var(--ok); border-color: rgba(39, 197, 138, 0.35); cursor: default; }
.wsd-btn:disabled { opacity: 0.7; cursor: default; }

.wsd-foot { margin-top: 18px; color: var(--ink-mute); font-size: 11.5px; display: flex; align-items: center; gap: 8px; }
.wsd-foot .live { width: 6px; height: 6px; border-radius: 50%; background: var(--ok); box-shadow: 0 0 8px var(--ok); }

@media (max-width: 820px) {
  .wsd-stats { grid-template-columns: repeat(2, 1fr); }
  .wsd-card { grid-template-columns: 44px 1fr; }
  .wsd-right { grid-column: 1 / -1; justify-content: space-between; flex-wrap: wrap; padding-top: 10px; border-top: 1px solid var(--line-soft); }
}
</style>
