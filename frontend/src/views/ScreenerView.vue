<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import type { ScreenerQuery, ScreenerResult, ScreenerSyncStats, ScreenerSyncStatus } from '../types'
import { renderMarkdown } from '../utils/markdown'

const running = ref(false)
const error = ref('')
const result = ref<ScreenerResult | null>(null)
const ran = ref(false)
const lastQuery = ref<ScreenerQuery>({})

const market = ref('')
const priceMin = ref('')
const priceMax = ref('')
const pctChgMin = ref('')
const pctChgMax = ref('')
const volumeRatioMin = ref('')
const turnoverMin = ref('')
const turnoverMax = ref('')
const mvMin = ref('')
const mvMax = ref('')
const peMin = ref('')
const peMax = ref('')
const aboveMa = ref('')
const keyword = ref('')

const syncStatus = ref<ScreenerSyncStatus | null>(null)
const syncLoading = ref(false)
const syncError = ref('')
const syncStats = ref<ScreenerSyncStats | null>(null)

const nlText = ref('')
const parseLoading = ref(false)
const parseSummary = ref('')
const parseError = ref('')

const interpretLoading = ref(false)
const interpretation = ref('')
const interpretModel = ref('')
const interpretError = ref('')

// v-model 在 type=number 输入框上会自动把非空值转成 number（Vue looseToNumber），
// 空值仍是 ''，因此入参须同时接受 string | number
function toNumber(value: string | number): number | undefined {
  if (value === '' || value == null) return undefined
  const num = Number(value)
  return Number.isFinite(num) ? num : undefined
}

function fmtDate(value: string | null | undefined): string {
  if (!value) return '—'
  const digits = value.replace(/-/g, '')
  if (digits.length !== 8) return value
  return `${digits.slice(0, 4)}-${digits.slice(4, 6)}-${digits.slice(6, 8)}`
}

const statusLine = computed(() => {
  const status = syncStatus.value
  if (!status) return ''
  if (!status.latest_bar_date) return '尚未同步全市场数据'
  return `数据截至 ${fmtDate(status.latest_bar_date)} · ${status.bar_symbols} 只 · 指标快照 ${status.basic_rows} 条`
})

async function loadSyncStatus() {
  try {
    syncStatus.value = await api.screenerSyncStatus()
  } catch {
    syncStatus.value = null
  }
}

async function syncNow() {
  if (syncLoading.value) return
  syncLoading.value = true
  syncError.value = ''
  syncStats.value = null
  try {
    syncStats.value = await api.screenerSync(20)
    result.value = null
    ran.value = false
    interpretation.value = ''
    interpretModel.value = ''
    await loadSyncStatus()
  } catch (err) {
    syncError.value = err instanceof Error ? err.message : String(err)
  } finally {
    syncLoading.value = false
  }
}

function applyQuery(query: ScreenerQuery) {
  market.value = query.market ?? ''
  priceMin.value = query.price_min != null ? String(query.price_min) : ''
  priceMax.value = query.price_max != null ? String(query.price_max) : ''
  pctChgMin.value = query.pct_chg_min != null ? String(query.pct_chg_min) : ''
  pctChgMax.value = query.pct_chg_max != null ? String(query.pct_chg_max) : ''
  volumeRatioMin.value = query.volume_ratio_min != null ? String(query.volume_ratio_min) : ''
  turnoverMin.value = query.turnover_rate_min != null ? String(query.turnover_rate_min) : ''
  turnoverMax.value = query.turnover_rate_max != null ? String(query.turnover_rate_max) : ''
  mvMin.value = query.total_mv_min != null ? String(query.total_mv_min) : ''
  mvMax.value = query.total_mv_max != null ? String(query.total_mv_max) : ''
  peMin.value = query.pe_min != null ? String(query.pe_min) : ''
  peMax.value = query.pe_max != null ? String(query.pe_max) : ''
  aboveMa.value = query.above_ma != null ? String(query.above_ma) : ''
  keyword.value = query.keyword ?? ''
}

function buildQuery(): ScreenerQuery {
  return {
    market: market.value || undefined,
    price_min: toNumber(priceMin.value),
    price_max: toNumber(priceMax.value),
    pct_chg_min: toNumber(pctChgMin.value),
    pct_chg_max: toNumber(pctChgMax.value),
    volume_ratio_min: toNumber(volumeRatioMin.value),
    turnover_rate_min: toNumber(turnoverMin.value),
    turnover_rate_max: toNumber(turnoverMax.value),
    total_mv_min: toNumber(mvMin.value),
    total_mv_max: toNumber(mvMax.value),
    pe_min: toNumber(peMin.value),
    pe_max: toNumber(peMax.value),
    above_ma: aboveMa.value ? Number(aboveMa.value) : undefined,
    keyword: String(keyword.value).trim() || undefined,
  }
}

async function parseNL() {
  const text = String(nlText.value).trim()
  if (!text || parseLoading.value) return
  parseLoading.value = true
  parseError.value = ''
  parseSummary.value = ''
  try {
    const parsed = await api.screenerParse(text)
    parseSummary.value = parsed.summary
    if (!parsed.fallback) applyQuery(parsed.query)
  } catch (err) {
    parseError.value = err instanceof Error ? err.message : String(err)
  } finally {
    parseLoading.value = false
  }
}

async function run() {
  if (running.value) return
  running.value = true
  error.value = ''
  interpretation.value = ''
  interpretModel.value = ''
  interpretError.value = ''
  const query = buildQuery()
  try {
    result.value = await api.runScreener(query)
    lastQuery.value = query
    ran.value = true
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    running.value = false
  }
}

async function interpret() {
  if (!result.value || !result.value.items.length || interpretLoading.value) return
  interpretLoading.value = true
  interpretError.value = ''
  try {
    const out = await api.screenerInterpret({
      query: lastQuery.value,
      universe: result.value.universe,
      total: result.value.total,
      items: result.value.items.slice(0, 15),
    })
    interpretation.value = out.interpretation
    interpretModel.value = out.model ?? ''
  } catch (err) {
    interpretError.value = err instanceof Error ? err.message : 'AI 解读失败'
  } finally {
    interpretLoading.value = false
  }
}

function pctTone(value: number | null): string {
  if (value == null || value === 0) return ''
  return value > 0 ? 'pnl-up' : 'pnl-down'
}

function formatPct(value: number | null): string {
  if (value == null) return '—'
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(2)}%`
}

function formatNum(value: number | null, digits = 2): string {
  return value == null ? '—' : value.toFixed(digits)
}

onMounted(loadSyncStatus)
</script>

<template>
  <section class="sc-wrap">
    <div class="sc-ambient" aria-hidden="true"></div>

    <header class="sc-toolbar">
      <div class="sc-title">
        <span class="sc-title-mark"></span>
        <div>
          <h2>技术选股</h2>
          <p>全市场筛选：价格 / 涨跌幅 / 量比 / 换手率 / 市值 / PE / 均线，支持一句话条件与 AI 解读</p>
        </div>
      </div>
      <div class="sc-actions">
        <span class="sc-status">{{ statusLine }}</span>
        <button type="button" class="btn-outline" :disabled="syncLoading" @click="syncNow">
          <span class="btn-spin" :class="{ spin: syncLoading }"></span>
          {{ syncLoading ? '同步中' : '同步全市场' }}
        </button>
        <button type="button" class="btn-outline" :disabled="running" @click="run">
          <span class="btn-spin" :class="{ spin: running }"></span>
          {{ running ? '筛选中' : '开始筛选' }}
        </button>
      </div>
    </header>

    <div v-if="syncError" class="notice notice-danger">
      <strong>同步失败</strong>
      <span>{{ syncError }}</span>
    </div>
    <p v-else-if="syncStats" class="sc-note">
      同步完成：新增 {{ syncStats.dates_fetched }} 个交易日 K 线（{{ syncStats.bars_upserted }} 行）、
      指标快照 {{ syncStats.basic_upserted }} 条、最新价 {{ syncStats.latest_prices_upserted }} 条，
      耗时 {{ syncStats.elapsed_seconds }}s。
      <template v-if="syncStats.errors.length"> 部分失败：{{ syncStats.errors.join('；') }}</template>
    </p>

    <div class="sc-nl">
      <input
        v-model="nlText"
        type="text"
        class="sc-input sc-nl-input"
        placeholder="一句话选股，如：量比大于2，换手率5%到15%，市值200亿以下，站上20日均线"
        @keyup.enter="parseNL"
      />
      <button type="button" class="btn-outline" :disabled="parseLoading || !nlText.trim()" @click="parseNL">
        <span class="btn-spin" :class="{ spin: parseLoading }"></span>
        {{ parseLoading ? '解析中' : 'AI 解析' }}
      </button>
    </div>
    <p v-if="parseSummary" class="sc-parse-line" :class="{ 'sc-parse-warn': parseSummary.startsWith('未能') }">
      {{ parseSummary }}
    </p>
    <div v-if="parseError" class="notice notice-danger">
      <strong>AI 解析失败</strong>
      <span>{{ parseError }}</span>
    </div>

    <div class="sc-config">
      <label class="sc-field">
        <span>市场</span>
        <select v-model="market" class="sc-input">
          <option value="">不限</option>
          <option value="SH">沪市</option>
          <option value="SZ">深市</option>
          <option value="BSE">北交所</option>
        </select>
      </label>
      <label class="sc-field">
        <span>价格下限</span>
        <input v-model="priceMin" type="number" min="0" step="0.01" class="sc-input" placeholder="可空" />
      </label>
      <label class="sc-field">
        <span>价格上限</span>
        <input v-model="priceMax" type="number" min="0" step="0.01" class="sc-input" placeholder="可空" />
      </label>
      <label class="sc-field">
        <span>涨跌幅下限 %</span>
        <input v-model="pctChgMin" type="number" step="0.01" class="sc-input" placeholder="如 0" />
      </label>
      <label class="sc-field">
        <span>涨跌幅上限 %</span>
        <input v-model="pctChgMax" type="number" step="0.01" class="sc-input" placeholder="可空" />
      </label>
      <label class="sc-field">
        <span>量比下限</span>
        <input v-model="volumeRatioMin" type="number" min="0" step="0.1" class="sc-input" placeholder="如 1.5" />
      </label>
      <label class="sc-field">
        <span>换手率下限 %</span>
        <input v-model="turnoverMin" type="number" min="0" step="0.1" class="sc-input" placeholder="如 5" />
      </label>
      <label class="sc-field">
        <span>换手率上限 %</span>
        <input v-model="turnoverMax" type="number" min="0" step="0.1" class="sc-input" placeholder="如 15" />
      </label>
      <label class="sc-field">
        <span>总市值下限（亿）</span>
        <input v-model="mvMin" type="number" min="0" step="1" class="sc-input" placeholder="可空" />
      </label>
      <label class="sc-field">
        <span>总市值上限（亿）</span>
        <input v-model="mvMax" type="number" min="0" step="1" class="sc-input" placeholder="如 200" />
      </label>
      <label class="sc-field">
        <span>PE(TTM) 下限</span>
        <input v-model="peMin" type="number" step="0.1" class="sc-input" placeholder="可空" />
      </label>
      <label class="sc-field">
        <span>PE(TTM) 上限</span>
        <input v-model="peMax" type="number" step="0.1" class="sc-input" placeholder="如 50" />
      </label>
      <label class="sc-field">
        <span>站上均线</span>
        <select v-model="aboveMa" class="sc-input">
          <option value="">不限</option>
          <option value="5">5 日均线</option>
          <option value="10">10 日均线</option>
          <option value="20">20 日均线</option>
        </select>
      </label>
      <label class="sc-field sc-field-wide">
        <span>关键词（代码/名称）</span>
        <input v-model="keyword" type="text" class="sc-input" placeholder="可空" />
      </label>
    </div>

    <div v-if="error" class="notice notice-danger">
      <strong>筛选失败</strong>
      <span>{{ error }}</span>
    </div>

    <template v-if="result">
      <div class="sc-summary">
        <div class="stat">
          <span class="stat-num">{{ result.universe }}</span>
          <span class="stat-label">候选池（最近同步交易日）</span>
        </div>
        <div class="stat">
          <span class="stat-num">{{ result.total }}</span>
          <span class="stat-label">命中</span>
        </div>
      </div>
      <p class="sc-note">候选池为最近同步交易日的全市场股票（点「同步全市场」更新）；均线条件需本地 K 线足够，不足则剔除。</p>

      <div v-if="result.items.length" class="sc-table-wrap">
        <table class="sc-table">
          <thead>
            <tr>
              <th>代码</th>
              <th>名称</th>
              <th>市场</th>
              <th class="num">最新价</th>
              <th class="num">涨跌幅</th>
              <th class="num">量比</th>
              <th class="num">换手率%</th>
              <th class="num">总市值(亿)</th>
              <th class="num">PE(TTM)</th>
              <th class="num">参考均线</th>
              <th>数据日期</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in result.items" :key="item.symbol">
              <td class="sc-symbol">{{ item.symbol }}</td>
              <td>{{ item.name }}</td>
              <td>{{ item.market || '—' }}</td>
              <td class="num">{{ formatNum(item.price) }}</td>
              <td class="num" :class="pctTone(item.pct_chg)">{{ formatPct(item.pct_chg) }}</td>
              <td class="num">{{ formatNum(item.volume_ratio) }}</td>
              <td class="num">{{ formatNum(item.turnover_rate) }}</td>
              <td class="num">{{ formatNum(item.total_mv_yi, 1) }}</td>
              <td class="num">{{ formatNum(item.pe_ttm, 1) }}</td>
              <td class="num">{{ formatNum(item.ma_value) }}</td>
              <td>{{ fmtDate(item.trade_date) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-else class="sc-state">没有符合条件的股票，可放宽条件或点「同步全市场」更新数据后重试。</div>

      <section v-if="result.items.length" class="sc-interpret">
        <header class="sc-interpret-head">
          <strong>AI 解读</strong>
          <button type="button" class="btn-outline" :disabled="interpretLoading" @click="interpret">
            <span class="btn-spin" :class="{ spin: interpretLoading }"></span>
            {{ interpretLoading ? '正在生成解读…' : interpretation ? '重新生成解读' : '生成大白话解读' }}
          </button>
        </header>
        <div v-if="interpretError" class="notice notice-danger">
          <strong>解读失败</strong>
          <span>{{ interpretError }}</span>
        </div>
        <div v-else-if="interpretation" class="sc-interpret-body">
          <div v-if="interpretModel" class="sc-interpret-model">由 {{ interpretModel }} 生成</div>
          <div class="md-body" v-html="renderMarkdown(interpretation)"></div>
        </div>
        <p v-else class="sc-note">点击「生成大白话解读」，AI 会解释这组条件在选什么样的股票，并逐只点评命中结果。</p>
      </section>
    </template>
    <div v-else-if="!ran && !error" class="sc-state">
      建议先点「同步全市场」拉取最近交易日行情，再用一句话描述条件或手动填写后「开始筛选」。留空的条件不参与过滤。
    </div>
  </section>
</template>

<style scoped>
.sc-wrap {
  position: relative;
  padding: 18px 20px 30px;
  text-align: left;
}

.sc-ambient {
  position: fixed;
  inset: 0;
  pointer-events: none;
  background:
    radial-gradient(1200px 500px at 85% -10%, rgba(213, 180, 106, 0.05), transparent 60%),
    radial-gradient(900px 400px at 0% 110%, rgba(55, 139, 255, 0.04), transparent 60%);
  z-index: 0;
}

.sc-toolbar {
  position: relative;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}

.sc-title {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}

.sc-title-mark {
  width: 3px;
  height: 34px;
  border-radius: 2px;
  background: linear-gradient(180deg, var(--gold-2), var(--gold-dim));
  flex: none;
  margin-top: 2px;
}

.sc-title h2 {
  margin: 0;
  font-size: 16px;
  color: var(--text);
  letter-spacing: 0.02em;
}

.sc-title p {
  margin: 4px 0 0;
  font-size: 12.5px;
  color: var(--muted);
}

.sc-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.sc-status {
  font-size: 12px;
  color: var(--muted);
}

.sc-nl {
  position: relative;
  display: flex;
  gap: 10px;
  margin-bottom: 8px;
}

.sc-nl-input {
  flex: 1;
}

.sc-parse-line {
  position: relative;
  margin: 0 0 10px;
  font-size: 12.5px;
  color: var(--gold-2);
}

.sc-parse-warn {
  color: var(--muted);
}

.sc-config {
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

.sc-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--muted);
}

.sc-field-wide {
  flex: 1 1 220px;
}

.sc-input {
  padding: 7px 10px;
  min-width: 96px;
  background: var(--bg-2);
  border: 1px solid var(--line);
  border-radius: 6px;
  color: var(--text);
  font-size: 12.5px;
  font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
  outline: none;
  transition: border-color 0.15s, box-shadow 0.15s;
}

.sc-input:focus {
  border-color: var(--gold);
  box-shadow: 0 0 0 2px rgba(213, 180, 106, 0.14);
}

select.sc-input {
  cursor: pointer;
}

.sc-summary {
  position: relative;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px;
  margin-bottom: 10px;
}

.sc-note {
  position: relative;
  margin: 0 0 12px;
  font-size: 11.5px;
  color: var(--muted);
}

.sc-table-wrap {
  position: relative;
  overflow-x: auto;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface);
  margin-bottom: 12px;
}

.sc-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
}

.sc-table th {
  position: sticky;
  top: 0;
  z-index: 2;
  background: var(--surface-3);
  color: var(--muted);
  font-weight: 600;
  text-align: left;
  padding: 9px 12px;
  border-bottom: 1px solid var(--line);
  white-space: nowrap;
}

.sc-table td {
  padding: 8px 12px;
  border-bottom: 1px solid var(--line);
  color: var(--text);
  white-space: nowrap;
}

.sc-table tbody tr:hover {
  background: rgba(255, 255, 255, 0.025);
}

.sc-table .num {
  text-align: right;
  font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
}

/* A 股红涨绿跌：本表 td 自带 color，需用更高特异性覆盖全局 .pnl-up/.pnl-down */
.sc-table td.pnl-up {
  color: var(--danger);
}

.sc-table td.pnl-down {
  color: var(--ok);
}

.sc-symbol {
  font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
  color: var(--gold-2);
}

.sc-state {
  position: relative;
  padding: 30px;
  text-align: center;
  color: var(--muted);
  font-size: 13px;
  border: 1px dashed var(--line);
  border-radius: var(--radius);
}

.sc-interpret {
  position: relative;
  margin-top: 16px;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface-2);
}

.sc-interpret-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}

.sc-interpret-head strong {
  color: var(--text);
  font-size: 13.5px;
}

.sc-interpret-body {
  border-top: 1px dashed var(--line);
  padding-top: 10px;
}

.sc-interpret-model {
  font-size: 11.5px;
  color: var(--muted);
  margin-bottom: 6px;
}
</style>
