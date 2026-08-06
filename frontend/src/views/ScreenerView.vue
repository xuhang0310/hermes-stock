<script setup lang="ts">
import { ref } from 'vue'
import { api } from '../api'
import type { ScreenerResult } from '../types'

const running = ref(false)
const error = ref('')
const result = ref<ScreenerResult | null>(null)
const ran = ref(false)

const market = ref('')
const priceMin = ref('')
const priceMax = ref('')
const pctChgMin = ref('')
const pctChgMax = ref('')
const volumeRatioMin = ref('')
const aboveMa = ref('')
const keyword = ref('')

// v-model 在 type=number 输入框上会自动把非空值转成 number（Vue looseToNumber），
// 空值仍是 ''，因此入参须同时接受 string | number
function toNumber(value: string | number): number | undefined {
  if (value === '' || value == null) return undefined
  const num = Number(value)
  return Number.isFinite(num) ? num : undefined
}

async function run() {
  if (running.value) return
  running.value = true
  error.value = ''
  try {
    result.value = await api.runScreener({
      market: market.value || undefined,
      price_min: toNumber(priceMin.value),
      price_max: toNumber(priceMax.value),
      pct_chg_min: toNumber(pctChgMin.value),
      pct_chg_max: toNumber(pctChgMax.value),
      volume_ratio_min: toNumber(volumeRatioMin.value),
      above_ma: aboveMa.value ? Number(aboveMa.value) : undefined,
      keyword: keyword.value.trim() || undefined,
    })
    ran.value = true
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    running.value = false
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
</script>

<template>
  <section class="sc-wrap">
    <div class="sc-ambient" aria-hidden="true"></div>

    <header class="sc-toolbar">
      <div class="sc-title">
        <span class="sc-title-mark"></span>
        <div>
          <h2>技术选股</h2>
          <p>价格 / 涨跌幅 / 量比 / 均线条件筛选（仅本地已缓存行情，不外呼拉全市场）</p>
        </div>
      </div>
      <button type="button" class="btn-outline" :disabled="running" @click="run">
        <span class="btn-spin" :class="{ spin: running }"></span>
        {{ running ? '筛选中' : '开始筛选' }}
      </button>
    </header>

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
          <span class="stat-label">候选池（本地有缓存价）</span>
        </div>
        <div class="stat">
          <span class="stat-num">{{ result.total }}</span>
          <span class="stat-label">命中</span>
        </div>
      </div>
      <p class="sc-note">候选池为本地已缓存最新价的股票，选股器不外呼拉取全市场；量比/均线需本地 K 线足够，不足则剔除。</p>

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
              <td class="num">{{ formatNum(item.ma_value) }}</td>
              <td>{{ item.trade_date || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-else class="sc-state">没有符合条件的股票，可放宽条件或补充本地行情缓存后重试。</div>
    </template>
    <div v-else-if="!ran && !error" class="sc-state">设置筛选条件后点击「开始筛选」。留空的条件不参与过滤。</div>
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
</style>
