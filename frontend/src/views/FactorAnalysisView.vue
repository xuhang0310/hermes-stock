<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import type { FactorBenchmarkItem } from '../types'

const loading = ref(false)
const running = ref(false)
const error = ref('')
const catalog = ref<Array<{ id: string; zoo: string; nickname: string | null }>>([])
const catalogLoading = ref(false)
const catalogError = ref('')
const selectedIds = ref(new Set<string>())
const results = ref<FactorBenchmarkItem[]>([])
const skipped = ref<Array<{ id: string; reason: string }>>([])
const dedup = ref<{ threshold: number; kept_ids: string[]; dropped: Array<{ id: string; max_corr_with_kept: number }> } | null>(null)
const coverage = ref('')
const methodology = ref('')
const symbolsInput = ref('600519.SH,000858.SZ,000001.SZ,600036.SH,601318.SH,000333.SZ,000651.SZ,600900.SH,601899.SH,600030.SH,600276.SH,600887.SH,601012.SH,300750.SZ,002594.SZ,002415.SZ,600809.SH,000568.SZ,601888.SH,600585.SH,600104.SH,601668.SH,000725.SZ,002475.SZ')
const startDate = ref('')
const endDate = ref('')
const dedupEnabled = ref(true)
const dedupThreshold = ref(0.7)
const copied = ref(false)
const filterQuery = ref('')
const zooFilter = ref<'all' | 'price' | 'alpha101' | 'qlib158' | 'gtja191' | 'academic' | 'fundamental'>('price')

const categoryLabels: Record<string, string> = { alive: '有效', reversed: '反转', dead: '失效' }
const categoryTone: Record<string, string> = { alive: 'pos', reversed: 'warn', dead: 'muted' }

const zooOptions = [
  { value: 'price', label: '量价可算（推荐）', zoos: ['alpha101', 'qlib158', 'gtja191'] },
  { value: 'alpha101', label: 'alpha101', zoos: ['alpha101'] },
  { value: 'qlib158', label: 'qlib158', zoos: ['qlib158'] },
  { value: 'gtja191', label: 'gtja191', zoos: ['gtja191'] },
  { value: 'all', label: '全部（含需额外数据）', zoos: null },
  { value: 'academic', label: 'academic（需长历史）', zoos: ['academic'] },
  { value: 'fundamental', label: 'fundamental（需基本面）', zoos: ['fundamental'] },
]

const symbols = computed(() =>
  symbolsInput.value
    .split(/[,，\s]+/)
    .map((s) => s.trim())
    .filter(Boolean)
)

const filteredCatalog = computed(() => {
  const option = zooOptions.find((o) => o.value === zooFilter.value)
  const q = filterQuery.value.trim().toLowerCase()
  return catalog.value.filter((f) => {
    if (option && option.zoos && !option.zoos.includes(f.zoo)) return false
    if (!q) return true
    return f.id.toLowerCase().includes(q) || (f.nickname || '').toLowerCase().includes(q) || f.zoo.toLowerCase().includes(q)
  })
})

const selectedCount = computed(() => selectedIds.value.size)

const canRun = computed(
  () => symbols.value.length >= 5 && selectedIds.value.size > 0 && selectedIds.value.size <= 20
)

const keptRows = computed<FactorBenchmarkItem[]>(() => {
  if (!dedup.value) return results.value
  const kept = new Set(dedup.value.kept_ids)
  return results.value.filter((r) => kept.has(r.id))
})

const droppedCount = computed(() => dedup.value?.dropped.length ?? 0)

function toggleFactor(id: string) {
  const next = new Set(selectedIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selectedIds.value = next
}

function selectAllVisible() {
  filteredCatalog.value.forEach((f) => {
    if (selectedIds.value.size < 20) selectedIds.value.add(f.id)
  })
  selectedIds.value = new Set(selectedIds.value)
}

function clearSelection() {
  selectedIds.value = new Set()
}

function clearResults() {
  results.value = []
  skipped.value = []
  dedup.value = null
}

function canRunMsg(): string {
  if (symbols.value.length < 5) return '至少选 5 只股票（建议 ≥20 只）'
  if (!selectedIds.value.size) return '请先勾选要评估的因子'
  if (selectedIds.value.size > 20) return '一次最多选 20 个因子'
  return ''
}

function skipReasonText(reason: string): string {
  if (reason.includes('panel missing required columns')) {
    const cols = reason.match(/\[([^\]]+)\]/)?.[1] ?? ''
    return `需基本面数据（${cols}），当前只有 K 线，无法计算`
  }
  if (reason.includes('nan_ratio')) {
    return '在该股票池/区间下几乎全是空值（数据覆盖不足）'
  }
  if (reason.includes('IC 序列为空')) {
    return '样本覆盖不足（股票数太少或区间太短），请加更多股票或扩大区间'
  }
  return reason
}

function percent(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '-'
  return `${(value * 100).toFixed(digits)}%`
}

function num(value: number | null | undefined, digits = 3): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '-'
  return Number(value).toFixed(digits)
}

function tone(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return ''
  if (value > 0) return 'pos'
  if (value < 0) return 'neg'
  return ''
}

async function loadCatalog() {
  catalogLoading.value = true
  catalogError.value = ''
  try {
    const res = await api.factorList({ limit: 200 })
    catalog.value = res.items.map((it) => ({ id: it.id, zoo: it.zoo, nickname: it.nickname ?? null }))
  } catch (reason) {
    catalogError.value = reason instanceof Error ? reason.message : '加载因子库失败'
  } finally {
    catalogLoading.value = false
  }
}

async function run() {
  running.value = true
  error.value = ''
  copied.value = false
  try {
    const res = await api.factorBenchmark({
      alpha_ids: Array.from(selectedIds.value),
      symbols: symbols.value,
      start_date: startDate.value || null,
      end_date: endDate.value || null,
      refresh: false,
      dedup_threshold: dedupEnabled.value ? dedupThreshold.value : null,
    })
    results.value = res.results
    skipped.value = res.skipped
    dedup.value = res.dedup
    methodology.value = res.methodology
    const cov = res.coverage
    coverage.value = `${cov.symbols.length} 只 · ${cov.start_date ?? '?'} - ${cov.end_date ?? '?'} · ${cov.dates} 个交易日`
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '因子评估失败'
  } finally {
    running.value = false
  }
}

async function copyFactorIds() {
  const ids = Array.from(selectedIds.value)
  if (!ids.length) return
  try {
    await navigator.clipboard.writeText(ids.join(','))
    copied.value = true
    window.setTimeout(() => (copied.value = false), 1600)
  } catch {
    /* 剪贴板不可用时静默失败 */
  }
}

onMounted(loadCatalog)
</script>

<template>
  <section class="fa-wrap">
    <div class="fa-ambient" aria-hidden="true"></div>

    <header class="fa-toolbar">
      <div class="fa-title">
        <span class="fa-title-mark"></span>
        <div>
          <h2>因子评估</h2>
          <p>横截面 IC / IR / 秩稳定性评估 + 低相关筛选（AlphaEval 思路）</p>
        </div>
      </div>
      <button type="button" class="btn-outline" :disabled="running || !canRun" :title="canRunMsg()" @click="run">
        <span class="btn-spin" :class="{ spin: running }"></span>
        {{ running ? '评估中' : '开始评估' }}
      </button>
    </header>

    <div class="fa-config">
      <label class="fa-field fa-field-wide">
        <span>股票池（逗号分隔）</span>
        <input v-model="symbolsInput" type="text" class="fa-input" placeholder="600519.SH,000001.SZ,..." />
      </label>
      <label class="fa-field">
        <span>起始日</span>
        <input v-model="startDate" type="text" class="fa-input" placeholder="YYYYMMDD（可空）" />
      </label>
      <label class="fa-field">
        <span>结束日</span>
        <input v-model="endDate" type="text" class="fa-input" placeholder="YYYYMMDD（可空）" />
      </label>
      <label class="fa-field fa-field-check">
        <input v-model="dedupEnabled" type="checkbox" />
        <span>低相关去重（阈值 {{ dedupThreshold }}）</span>
      </label>
    </div>

    <div v-if="error" class="notice notice-danger">
      <strong>评估失败</strong>
      <span>{{ error }}</span>
    </div>

    <section class="fa-picker">
      <header class="fa-picker-head">
        <div>
          <strong>选择因子（{{ selectedCount }}/20，一次评估最多 20 个）</strong>
          <span v-if="catalogError" class="fa-picker-err">{{ catalogError }}</span>
        </div>
        <div class="fa-picker-actions">
          <label class="fa-search">
            <input v-model="filterQuery" type="text" class="fa-input" placeholder="搜索因子 ID / 昵称" />
          </label>
          <select v-model="zooFilter" class="fa-input fa-zoo-select">
            <option v-for="opt in zooOptions" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
          </select>
          <button type="button" class="btn-outline" :disabled="selectedCount >= 20" @click="selectAllVisible">全选当前</button>
          <button type="button" class="btn-outline" :disabled="!selectedCount" @click="clearSelection">清空</button>
        </div>
      </header>
      <div v-if="catalogLoading" class="fa-state fa-state-sm">正在加载因子库...</div>
      <div v-else-if="filteredCatalog.length" class="fa-picker-grid">
        <label
          v-for="f in filteredCatalog"
          :key="f.id"
          class="fa-picker-chip"
          :class="{ picked: selectedIds.has(f.id) }"
        >
          <input type="checkbox" :checked="selectedIds.has(f.id)" @change="toggleFactor(f.id)" />
          <span class="fa-chip-id">{{ f.id }}</span>
          <span class="fa-chip-meta">{{ f.zoo }}<template v-if="f.nickname"> · {{ f.nickname }}</template></span>
        </label>
      </div>
      <div v-else class="fa-state fa-state-sm">没有匹配的因子</div>
    </section>

    <template v-if="results.length">
      <div class="fa-summary">
        <div class="stat">
          <span class="stat-num">{{ results.length }}</span>
          <span class="stat-label">评估因子</span>
        </div>
        <div class="stat">
          <span class="stat-num">{{ keptRows.length }}</span>
          <span class="stat-label">去重后保留</span>
        </div>
        <div class="stat">
          <span class="stat-num">{{ skipped.length }}</span>
          <span class="stat-label">跳过（样本不足）</span>
        </div>
        <div class="stat">
          <span class="stat-num stat-num-sm">{{ coverage }}</span>
          <span class="stat-label">样本范围</span>
        </div>
      </div>

      <div class="fa-table-wrap">
        <table class="fa-table">
          <thead>
            <tr>
              <th>因子 ID</th>
              <th>来源</th>
              <th class="num">IC 均值</th>
              <th class="num">ICIR</th>
              <th class="num">IC 正值比</th>
              <th class="num">秩稳定 RRE</th>
              <th class="num">train IC</th>
              <th class="num">test IC</th>
              <th>判定</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="it in results" :key="it.id" :class="{ dropped: dedup && !dedup.kept_ids.includes(it.id) }">
              <td class="fa-id">{{ it.id }}</td>
              <td>{{ it.zoo }}</td>
              <td class="num" :class="tone(it.ic_mean)">{{ percent(it.ic_mean) }}</td>
              <td class="num" :class="tone(it.ir)">{{ num(it.ir) }}</td>
              <td class="num">{{ percent(it.ic_positive_ratio, 1) }}</td>
              <td class="num" :class="it.rank_stability >= 0.7 ? 'pos' : ''">{{ num(it.rank_stability) }}</td>
              <td class="num" :class="tone(it.train_ic)">{{ percent(it.train_ic ?? 0) }}</td>
              <td class="num" :class="tone(it.test_ic)">{{ percent(it.test_ic ?? 0) }}</td>
              <td><span class="fa-cat" :class="categoryTone[it.category]">{{ categoryLabels[it.category] }}</span></td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="dedup && dedup.dropped.length" class="fa-dedup">
        <strong>低相关去重 · {{ dedup.dropped.length }} 个因子因与已选集相关 ≥ {{ dedup.threshold }} 被剔除：</strong>
        <span class="fa-dedup-tags">
          <span v-for="d in dedup.dropped" :key="d.id" class="fa-dedup-tag" :title="`与已选集最大相关 ${(d.max_corr_with_kept * 100).toFixed(1)}%`">
            {{ d.id }} ({{ (d.max_corr_with_kept * 100).toFixed(0) }}%)
          </span>
        </span>
      </div>

      <div v-if="skipped.length" class="fa-skipped">
        <strong>以下 {{ skipped.length }} 个因子无法评估（不算失败，是数据覆盖问题）：</strong>
        <ul class="fa-skipped-list">
          <li v-for="s in skipped" :key="s.id">
            <code>{{ s.id }}</code> — {{ skipReasonText(s.reason) }}
          </li>
        </ul>
        <p class="fa-skipped-hint">提示：换用左侧"量价可算"来源筛选，并加更多股票（≥20 只）可减少跳过。</p>
      </div>

      <div class="fa-actions">
        <span class="fa-selected">{{ selectedCount }} 个因子已选</span>
        <button type="button" class="btn-outline" :disabled="!selectedCount" @click="copyFactorIds">
          {{ copied ? '已复制 ✓' : '复制因子 ID 列表' }}
        </button>
        <span class="fa-hint">复制后粘贴到问股，输入"用这些因子跑组合回测"</span>
      </div>

      <p class="fa-methodology">{{ methodology }}</p>
    </template>

    <div v-else class="fa-state">{{ results.length ? '' : '勾选因子后点右上角"开始评估"，结果会显示在这里' }}</div>
  </section>
</template>

<style scoped>
.fa-wrap {
  position: relative;
  padding: 18px 20px 30px;
  text-align: left;
}

.fa-ambient {
  position: fixed;
  inset: 0;
  pointer-events: none;
  background:
    radial-gradient(1200px 500px at 85% -10%, rgba(213, 180, 106, 0.05), transparent 60%),
    radial-gradient(900px 400px at 0% 110%, rgba(55, 139, 255, 0.04), transparent 60%);
  z-index: 0;
}

.fa-toolbar {
  position: relative;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}

.fa-title {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}

.fa-title-mark {
  width: 3px;
  height: 34px;
  border-radius: 2px;
  background: linear-gradient(180deg, var(--gold-2), var(--gold-dim));
  flex: none;
  margin-top: 2px;
}

.fa-title h2 {
  margin: 0;
  font-size: 16px;
  color: var(--text);
  letter-spacing: 0.02em;
}

.fa-title p {
  margin: 4px 0 0;
  font-size: 12.5px;
  color: var(--muted);
}

.fa-config {
  position: relative;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: flex-end;
  padding: 12px;
  margin-bottom: 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface-2);
}

.fa-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--muted);
}

.fa-field-wide {
  flex: 1 1 320px;
}

.fa-input {
  padding: 7px 10px;
  background: var(--bg-2);
  border: 1px solid var(--line);
  border-radius: 6px;
  color: var(--text);
  font-size: 12.5px;
  font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
  outline: none;
  transition: border-color 0.15s, box-shadow 0.15s;
}

.fa-input:focus {
  border-color: var(--gold);
  box-shadow: 0 0 0 2px rgba(213, 180, 106, 0.14);
}

.fa-field-check {
  flex-direction: row;
  align-items: center;
  gap: 6px;
  padding: 9px 0;
  cursor: pointer;
}

.fa-field-check input {
  accent-color: var(--gold);
}

.fa-picker {
  position: relative;
  padding: 12px;
  margin-bottom: 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface-2);
}

.fa-picker-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.fa-picker-head strong {
  font-size: 13px;
  color: var(--text);
}

.fa-picker-err {
  margin-left: 8px;
  font-size: 12px;
  color: var(--danger);
}

.fa-picker-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.fa-search .fa-input {
  width: 240px;
}

.fa-picker-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
  gap: 6px;
  max-height: 260px;
  overflow-y: auto;
  padding-right: 4px;
}

.fa-picker-chip {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 6px 9px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--surface);
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
  min-width: 0;
}

.fa-picker-chip:hover {
  border-color: var(--line-strong);
}

.fa-picker-chip.picked {
  border-color: rgba(213, 180, 106, 0.55);
  background: rgba(213, 180, 106, 0.07);
}

.fa-picker-chip input {
  accent-color: var(--gold);
  flex: none;
}

.fa-chip-id {
  font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
  font-size: 12px;
  color: var(--gold-2);
  flex: none;
}

.fa-chip-meta {
  font-size: 11px;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.fa-state-sm {
  padding: 14px;
  font-size: 12px;
}

.fa-zoo-select {
  width: auto;
  min-width: 150px;
  cursor: pointer;
}

.fa-skipped {
  position: relative;
  margin-bottom: 12px;
  padding: 12px;
  border: 1px solid rgba(255, 93, 93, 0.25);
  border-radius: var(--radius);
  background: rgba(255, 93, 93, 0.04);
  font-size: 12.5px;
}

.fa-skipped strong {
  color: var(--warning);
}

.fa-skipped-list {
  margin: 8px 0 0;
  padding-left: 18px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 180px;
  overflow-y: auto;
}

.fa-skipped-list code {
  color: var(--danger);
  font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
  font-size: 11.5px;
}

.fa-skipped-hint {
  margin: 8px 0 0;
  font-size: 11.5px;
  color: var(--muted);
}

.fa-summary {
  position: relative;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px;
  margin-bottom: 14px;
}

.stat {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface-2);
}

.stat-num {
  font-size: 20px;
  font-weight: 700;
  color: var(--text);
}

.stat-num-sm {
  font-size: 13px;
  font-weight: 600;
  word-break: break-all;
}

.stat-label {
  font-size: 11.5px;
  color: var(--muted);
}

.fa-table-wrap {
  position: relative;
  overflow-x: auto;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface);
  margin-bottom: 12px;
}

.fa-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
}

.fa-table th {
  position: sticky;
  top: 0;
  z-index: 2;
  background: var(--surface-3);
  color: var(--muted);
  font-weight: 600;
  text-align: left;
  padding: 9px 12px;
  border-bottom: 1px solid var(--line-strong);
  white-space: nowrap;
}

.fa-table td {
  padding: 8px 12px;
  border-bottom: 1px solid var(--line);
  color: var(--text);
  white-space: nowrap;
}

.fa-table tbody tr:hover {
  background: rgba(255, 255, 255, 0.025);
}

.fa-table tbody tr.selected {
  background: rgba(213, 180, 106, 0.06);
}

.fa-table tbody tr.dropped {
  opacity: 0.45;
}

.fa-table .num {
  text-align: right;
  font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
}

.fa-check-cell {
  width: 32px;
  padding: 0 8px !important;
}

.fa-check {
  display: grid;
  place-items: center;
}

.fa-check input {
  accent-color: var(--gold);
  cursor: pointer;
}

.fa-id {
  color: var(--gold-2);
  font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
}

.fa-cat {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--line);
}

.fa-cat.pos { color: var(--ok); border-color: rgba(39, 197, 138, 0.35); }
.fa-cat.warn { color: var(--warning); border-color: rgba(213, 166, 66, 0.35); }
.fa-cat.muted { color: var(--muted); }

.pos { color: var(--ok) !important; }
.neg { color: var(--danger) !important; }

.fa-dedup {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  margin-bottom: 12px;
  border: 1px solid rgba(213, 180, 106, 0.3);
  border-radius: var(--radius);
  background: rgba(213, 180, 106, 0.05);
  font-size: 12.5px;
}

.fa-dedup strong {
  color: var(--gold-2);
}

.fa-dedup-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.fa-dedup-tag {
  font-size: 11.5px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--line-strong);
  color: var(--text);
  background: rgba(255, 255, 255, 0.03);
}

.fa-actions {
  position: relative;
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.fa-selected {
  font-size: 13px;
  color: var(--gold-2);
  font-weight: 600;
}

.fa-hint {
  font-size: 11.5px;
  color: var(--muted);
}

.fa-methodology {
  position: relative;
  margin: 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--muted);
  border-top: 1px dashed var(--line);
  padding-top: 10px;
}

.fa-state {
  position: relative;
  padding: 30px;
  text-align: center;
  color: var(--muted);
  font-size: 13px;
  border: 1px dashed var(--line);
  border-radius: var(--radius);
}

.btn-outline {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 8px 14px;
  border-radius: 6px;
  border: 1px solid var(--line-strong);
  background: transparent;
  color: var(--text);
  font-size: 13px;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
}

.btn-outline:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.05);
  border-color: var(--gold);
}

.btn-outline:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-spin {
  width: 11px;
  height: 11px;
  border-radius: 50%;
  border: 2px solid currentColor;
  border-top-color: transparent;
}

.btn-spin.spin {
  animation: fa-spin 0.7s linear infinite;
}

@keyframes fa-spin {
  to { transform: rotate(360deg); }
}

.notice {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 12px 14px;
  margin-bottom: 14px;
  border-radius: var(--radius);
  font-size: 12.5px;
}

.notice-danger {
  border: 1px solid rgba(255, 93, 93, 0.35);
  background: rgba(255, 93, 93, 0.08);
  color: var(--danger);
}

@media (prefers-reduced-motion: reduce) {
  .btn-spin.spin { animation: none; }
}
</style>